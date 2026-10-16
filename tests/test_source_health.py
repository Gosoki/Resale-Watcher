"""数据源「悄悄瞎掉」的两道防线：解析突然失效的判定 + 持续失败的推送告警。

【起因】2026-10 ヤフオク 把搜索页换成了新版，旧解析器一件商品都认不出来，却照常返回
「0 件、已到底」。日志里一行行「在售84件 新增0」看着一切正常；而售出对账把库里所有在售商品
当成失踪、每轮拉详情去核实 —— 9 小时 1595 个请求，占全部请求的六成。没有任何东西主动告诉你。

  1. 上一轮平台还报着一堆、这一轮一件都没解析出来 → 抛 SourceBroken，本轮不做售出对账
  2. 某条规则在某个源上连续失败超过 source_alert_min 分钟 → 推送告警（按源去重）；恢复时补一条
"""
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import sources  # noqa: E402
from core import poller  # noqa: E402

RULE = {
    "id": 1, "name": "测试", "keyword": "RTX 5090", "sources": "",
    "include_all": "5090", "include_any": "", "exclude_any": "", "warn_desc": "",
    "price_min": 0, "price_max": 0, "condition_ids": "", "allow_shops": 1,
    "check_desc": 0, "deal_ratio": 90, "quick_min": 7,
    "max_pages": 5, "detail_budget": 10, "missing_grace_min": 20, "sold_scan_hours": 24,
}


class FakeStore:
    def __init__(self, live=0, settings=None, last_total=None):
        self.live = [{"item_id": f"m{i}", "status": "on_sale", "matched": 1, "price": 1,
                      "last_seen_at": config.now() - timedelta(hours=1)} for i in range(live)]
        self.settings = settings or {}
        self.last_total = last_total
        self.state_updates = []

    def get_settings(self, force=False):
        return self.settings

    def get_source_state(self, rid, source):
        # 成交轮刚跑过（不到点），在售扫描到点了
        return {"last_scan_at": None, "last_sold_at": config.now(), "last_total": self.last_total}

    def on_sale_items(self, rule_id, source):
        return list(self.live)

    def update_source_state(self, *a, **k):
        self.state_updates.append(k)

    def touch_seen(self, *a, **k):
        pass

    def update_tracked(self, *a, **k):
        pass


class PageSource:
    """按给定的页返回；默认是解析失效时的样子：请求成功了，返回空列表、总数 0。"""
    key, name = "fake", "测试源"

    def __init__(self, page=None):
        self.page = page or {"items": [], "next": "", "total": 0}
        self.detail_calls = []

    def search(self, keyword, *, sold=False, page_token=""):
        return dict(self.page)

    def can_detail(self, iid):
        return True

    def detail(self, iid):
        self.detail_calls.append(iid)
        return {"status": "on_sale", "price": 1, "name": "", "description": "", "bid_count": None}


# ---------------------------------------------------------------- 1. 解析突然失效

def test_上一轮还有一堆_这一轮一件都没有_当成解析坏了_不做售出对账(monkeypatch):
    fs, src = FakeStore(live=30), PageSource()
    monkeypatch.setattr(poller, "store", fs)
    with pytest.raises(sources.SourceBroken):
        poller.scan_on_sale(src, RULE, prev_total=84)
    assert src.detail_calls == [], "当成失踪去核实，正是这次白烧六成请求的那条路"
    assert fs.state_updates == [], "也不该把 last_total 写成 0 —— 下一轮还要拿上一轮的数来比"


def test_没有上一轮的数或者本来就很少时_照常对账(monkeypatch):
    # 冷门关键词真的卖空了是可能的；只有「从一堆突然掉到 0」才可疑
    for prev in (None, 0, poller.SUSPECT_DROP_FROM - 1):
        fs, src = FakeStore(live=3), PageSource()
        monkeypatch.setattr(poller, "store", fs)
        poller.scan_on_sale(src, RULE, prev_total=prev)
        assert len(src.detail_calls) == 3, f"prev_total={prev} 时应照常核实失踪商品"


def test_一页全是フリマ_过滤后为空但解析是好的_不算失效(monkeypatch):
    # ヤフオク 在源里滤掉 フリマ 商品：items 为空、raw 记着过滤前拿到了几个 ID
    fs, src = FakeStore(live=3), PageSource({"items": [], "next": "", "total": 12, "raw": 12})
    monkeypatch.setattr(poller, "store", fs)
    stat = poller.scan_on_sale(src, RULE, prev_total=12)
    assert fs.state_updates and fs.state_updates[-1]["last_total"] == 12
    assert stat["parsed"] is True, "解析到了 ID，健康检查应当算它成功"


def test_平台明确说0件_是真没货_不算失效(monkeypatch):
    fs, src = FakeStore(live=3), PageSource({"items": [], "next": "", "total": 0, "empty_ok": True})
    monkeypatch.setattr(poller, "store", fs)
    poller.scan_on_sale(src, RULE, prev_total=84)
    assert len(src.detail_calls) == 3, "真卖空了就该去核实，库里那几件多半已经卖掉"


def test_扫到0件的成功_不能拿来证明源是好的(monkeypatch):
    fs, src = FakeStore(), PageSource()
    monkeypatch.setattr(poller, "store", fs)
    assert poller.scan_on_sale(src, RULE, prev_total=3)["parsed"] is False


# ---------------------------------------------------------------- 主循环接线（真跑 _run_round）

def _round(monkeypatch, src, fs, rules=(RULE,)):
    monkeypatch.setattr(poller, "store", fs)
    monkeypatch.setattr(poller.sources, "for_rule", lambda r: [src])
    monkeypatch.setattr(poller, "_alerted", {})
    monkeypatch.setattr(poller, "finalize", lambda rid: {})
    poller._run_round(list(rules), threading.Event())


def test_主循环_解析失效时不对账_推进时间戳_记一次失败(monkeypatch):
    monkeypatch.setattr(poller, "_health", {})
    fs, src = FakeStore(live=30, last_total=84, settings={"source_alert_min": 30}), PageSource()
    _round(monkeypatch, src, fs)
    assert src.detail_calls == []
    assert any("last_scan_at" in u for u in fs.state_updates), \
        "不推进 last_scan_at 的话 _due 恒为真，30 秒后又来一次 —— 又是白烧请求"
    assert poller._health[(src.key, RULE["id"])]["fails"] == 1


def test_主循环_扫描成功才清掉失败记录(monkeypatch):
    ok = PageSource({"items": [{"source": "fake", "item_id": "x1", "name": "RTX 5090", "price": 1,
                                "status": "on_sale", "condition_id": None, "item_type": "user",
                                "category_id": None, "brand_name": "", "seller_id": "", "thumb_url": "",
                                "listed_at": None, "updated_at_src": None, "end_time": None,
                                "bid_count": None, "buy_now_price": None}],
                     "next": "", "total": 1})
    monkeypatch.setattr(poller, "_health", {(ok.key, RULE["id"]): {
        "since": config.now(), "last": config.now(), "fails": 1, "err": "x"}})
    fs = FakeStore(settings={"source_alert_min": 30})
    fs.upsert_item = lambda rid, snap, verdict: {"new": False, "price_changed": False, "old_price": 1}
    monkeypatch.setattr(poller, "fetch_details", lambda src, rule: 0)
    _round(monkeypatch, ok, fs)
    assert (ok.key, RULE["id"]) not in poller._health


# ---------------------------------------------------------------- 2. 持续失败告警

class Src:
    key, name = "yahoo_auction", "ヤフオク"


R1 = dict(RULE, id=1, name="RTX 4090 单卡", quick_min=7)
R2 = dict(RULE, id=2, name="RTX 5090 单卡", quick_min=7)


@pytest.fixture
def clock(monkeypatch):
    """可拨动的时钟 + 干净的健康状态 + 收集推送。"""
    t = {"now": config.now()}
    monkeypatch.setattr(poller.config, "now", lambda: t["now"])
    monkeypatch.setattr(poller, "_health", {})
    monkeypatch.setattr(poller, "_alerted", {})
    sent = []
    fs = FakeStore(settings={"source_alert_min": 30, "notify_url": "https://example.invalid/hook",
                             "notify_body": ""})
    monkeypatch.setattr(poller, "store", fs)

    def tick(minutes):
        t["now"] += timedelta(minutes=minutes)
    return tick, sent, fs


def _fail(sent, rule=R1, err=None, alert=None):
    poller._source_failed(Src(), rule, err or sources.SourceBroken("解析不出来"),
                          alert=alert or (lambda url, body, text: sent.append(text)))


def _ok(sent, rule=R1, alert=None):
    poller._source_ok(Src(), rule, alert=alert or (lambda url, body, text: sent.append(text)))


def test_偶尔失败一两次不告警(clock):
    tick, sent, _ = clock
    _fail(sent)
    tick(10)
    _fail(sent)
    tick(5)
    _ok(sent)
    tick(31)
    _fail(sent)                              # 成功之后的又一次偶发，必须从头计
    assert sent == [], "下一轮就好了的抖动不值得推送"


def test_持续失败超过阈值_只告警一次(clock):
    tick, sent, _ = clock
    for _ in range(7):                       # 每 7 分钟失败一次，共 42 分钟
        _fail(sent)
        tick(7)
    assert len(sent) == 1
    assert "ヤフオク" in sent[0] and "改版" in sent[0] and "RTX 4090 单卡" in sent[0]


def test_中间根本没在抓的空档_不算进抓不到的时长(clock):
    # 待命、电脑睡眠、库连不上、配额用完：两次失败之间隔了很久，从头计
    tick, sent, _ = clock
    _fail(sent)
    tick(10 * 60)
    _fail(sent)
    assert sent == [], "不能推出「已经 600 分钟抓不到东西」"


def test_一直不恢复_6小时后再提醒一次(clock):
    tick, sent, _ = clock
    for _ in range(6):                       # t=0〜35，第 6 次（35 分钟）告警
        _fail(sent)
        tick(7)
    assert len(sent) == 1
    for _ in range(int(5 * 60 / 7)):         # 5 小时内一直失败
        _fail(sent)
        tick(7)
    assert len(sent) == 1, "6 小时内不重复"
    for _ in range(12):
        _fail(sent)
        tick(7)
    assert len(sent) == 2


def test_同源别的规则成功_不会掩盖这条规则的持续失败(clock):
    tick, sent, _ = clock
    for _ in range(6):
        _fail(sent, R1)
        _ok(sent, R2)                        # 同一个源上另一条规则一直好好的
        tick(7)
    assert len(sent) == 1, "按源记的话 R2 每次成功都会把 R1 的失败清零，永远不告警"


def test_同源所有规则都恢复了才报已恢复(clock):
    tick, sent, _ = clock
    for _ in range(6):
        _fail(sent, R1)
        _fail(sent, R2)
        tick(7)
    assert len(sent) == 1 and "同源还有 1 条规则也在失败" in sent[0]
    _ok(sent, R1)
    assert len(sent) == 1, "R2 还坏着，不该报恢复"
    _ok(sent, R2)
    assert len(sent) == 2 and sent[1].startswith("✅")


def test_告警推送瞬时失败_下一次失败时重发(clock):
    tick, sent, _ = clock
    from core.notify import PushFailed

    def flaky(url, body, text):
        raise PushFailed("HTTP 503", transient=True)
    for _ in range(6):                       # 第 6 次到了阈值，但推送 503 了
        _fail(sent, alert=flaky)
        tick(7)
    assert sent == []
    _fail(sent)                              # 下一次失败时应当重发，而不是等 6 小时
    assert len(sent) == 1, "一次 503 不该把提醒拖到 6 小时以后"


def test_恢复消息发不出去_下次成功时补发(clock):
    tick, sent, _ = clock
    from core.notify import PushFailed
    for _ in range(6):
        _fail(sent)
        tick(7)
    assert len(sent) == 1

    def flaky(url, body, text):
        raise PushFailed("网络错误", transient=True)
    _ok(sent, alert=flaky)
    _ok(sent)
    assert len(sent) == 2 and sent[1].startswith("✅")


def test_限流和其他报错的告警文案不一样(clock):
    tick, sent, _ = clock
    for _ in range(6):
        _fail(sent, err=sources.RateLimited("HTTP 403"))
        tick(7)
    assert "限流" in sent[0] and "改版" not in sent[0]


def test_数据库报错不算源的故障(clock):
    tick, sent, _ = clock
    import pymysql
    for _ in range(10):
        _fail(sent, err=pymysql.err.OperationalError(2013, "Lost connection"))
        tick(7)
    assert sent == [] and not poller._health, "库连不上不该推「平台可能改了接口」"


@pytest.mark.parametrize("limit", [0, -1])
def test_阈值为0或负数就是关(clock, limit):
    tick, sent, fs = clock
    fs.settings["source_alert_min"] = limit
    for _ in range(20):
        _fail(sent)
        tick(7)
    assert sent == []


def test_推送发不出去也不能把轮询带崩(clock):
    tick, _, _ = clock

    def boom(*a):
        raise RuntimeError("webhook 挂了")
    for _ in range(6):
        _fail([], alert=boom)                # 不抛就算过
        tick(7)
