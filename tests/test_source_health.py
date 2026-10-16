"""数据源「悄悄瞎掉」的两道防线：解析突然失效的判定 + 持续失败的推送告警。

【起因】2026-10 ヤフオク 把搜索页换成了新版，旧解析器一件商品都认不出来，却照常返回
「0 件、已到底」。日志里一行行「在售84件 新增0」看着一切正常；而售出对账把库里所有在售商品
当成失踪、每轮拉详情去核实 —— 9 小时 1595 个请求，占全部请求的六成。没有任何东西主动告诉你。

  1. 上一轮平台还报着一堆、这一轮一件都没解析出来 → 抛 SourceBroken，本轮不做售出对账
  2. 某个源连续失败超过 source_alert_min 分钟 → 推送一条告警；恢复时补一条「已恢复」
"""
import sys
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
    "max_pages": 5, "detail_budget": 10, "missing_grace_min": 20,
}


class FakeStore:
    def __init__(self, live=0, settings=None):
        self.live = [{"item_id": f"m{i}", "status": "on_sale", "matched": 1, "price": 1,
                      "last_seen_at": config.now() - timedelta(hours=1)} for i in range(live)]
        self.settings = settings or {}
        self.state_updates = []

    def get_settings(self, force=False):
        return self.settings

    def on_sale_items(self, rule_id, source):
        return list(self.live)

    def update_source_state(self, *a, **k):
        self.state_updates.append(k)

    def touch_seen(self, *a, **k):
        pass

    def update_tracked(self, *a, **k):
        pass


class EmptySource:
    """解析失效时的样子：请求成功了，返回的却是空列表、总数 0。"""
    key, name = "fake", "测试源"

    def __init__(self):
        self.detail_calls = []

    def search(self, keyword, *, sold=False, page_token=""):
        return {"items": [], "next": "", "total": 0}

    def can_detail(self, iid):
        return True

    def detail(self, iid):
        self.detail_calls.append(iid)
        return {"status": "on_sale", "price": 1, "name": "", "description": "", "bid_count": None}


# ---------------------------------------------------------------- 1. 解析突然失效

def test_上一轮还有一堆_这一轮一件都没有_当成解析坏了_不做售出对账(monkeypatch):
    fs, src = FakeStore(live=30), EmptySource()
    monkeypatch.setattr(poller, "store", fs)
    with pytest.raises(sources.SourceBroken):
        poller.scan_on_sale(src, RULE, prev_total=84)
    assert src.detail_calls == [], "当成失踪去核实，正是这次白烧六成请求的那条路"
    assert fs.state_updates == [], "也不该把 last_total 写成 0 —— 下一轮还要拿上一轮的数来比"


def test_没有上一轮的数或者本来就很少时_照常对账(monkeypatch):
    # 冷门关键词真的卖空了是可能的；只有「从一堆突然掉到 0」才可疑
    for prev in (None, 0, poller.SUSPECT_DROP_FROM - 1):
        fs, src = FakeStore(live=3), EmptySource()
        monkeypatch.setattr(poller, "store", fs)
        poller.scan_on_sale(src, RULE, prev_total=prev)
        assert len(src.detail_calls) == 3, f"prev_total={prev} 时应照常核实失踪商品"


def test_常驻轮询和面板手动跑都把上一轮的总数传进去():
    import inspect
    assert 'prev_total=st.get("last_total")' in inspect.getsource(poller._run_round)
    assert "prev_total=prev" in inspect.getsource(poller.run_once)


# ---------------------------------------------------------------- 2. 持续失败告警

class Src:
    key, name = "yahoo_auction", "ヤフオク"


@pytest.fixture
def clock(monkeypatch):
    """可拨动的时钟 + 干净的健康状态 + 收集推送。"""
    t = {"now": config.now()}
    monkeypatch.setattr(poller.config, "now", lambda: t["now"])
    monkeypatch.setattr(poller, "_health", {})
    sent = []
    fs = FakeStore(settings={"source_alert_min": 30, "notify_url": "https://example.invalid/hook",
                             "notify_body": ""})
    monkeypatch.setattr(poller, "store", fs)

    def tick(minutes):
        t["now"] += timedelta(minutes=minutes)
    return tick, sent, fs


def _fail(sent, err=None):
    poller._source_failed(Src(), err or sources.SourceBroken("解析不出来"),
                          alert=lambda url, body, text: sent.append(text))


def _ok(sent):
    poller._source_ok(Src(), alert=lambda url, body, text: sent.append(text))


def test_偶尔失败一两次不告警(clock):
    tick, sent, _ = clock
    _fail(sent)
    tick(10)
    _fail(sent)
    tick(5)
    _ok(sent)
    assert sent == [], "下一轮就好了的抖动不值得推送"


def test_持续失败超过阈值_只告警一次(clock):
    tick, sent, _ = clock
    for _ in range(7):                       # 每 7 分钟失败一次，共 42 分钟
        _fail(sent)
        tick(7)
    assert len(sent) == 1
    assert "ヤフオク" in sent[0] and "改版" in sent[0], "SourceBroken 要明确说是改版"


def test_一直不恢复_6小时后再提醒一次(clock):
    tick, sent, _ = clock
    _fail(sent)
    tick(31)
    _fail(sent)
    assert len(sent) == 1
    tick(60 * 5)
    _fail(sent)
    assert len(sent) == 1, "6 小时内不重复"
    tick(61)
    _fail(sent)
    assert len(sent) == 2


def test_告过警之后恢复_补一条已恢复(clock):
    tick, sent, _ = clock
    _fail(sent)
    tick(31)
    _fail(sent)
    _ok(sent)
    assert len(sent) == 2 and sent[1].startswith("✅")
    # 恢复后再坏，重新计时
    _fail(sent)
    tick(10)
    _fail(sent)
    assert len(sent) == 2


def test_限流和其他报错的告警文案不一样(clock):
    tick, sent, _ = clock
    _fail(sent, sources.RateLimited("HTTP 403"))
    tick(31)
    _fail(sent, sources.RateLimited("HTTP 403"))
    assert "限流" in sent[0] and "改版" not in sent[0]


def test_阈值为0就不告警(clock):
    tick, sent, fs = clock
    fs.settings["source_alert_min"] = 0
    _fail(sent)
    tick(600)
    _fail(sent)
    assert sent == []


def test_推送发不出去也不能把轮询带崩(clock):
    tick, _, _ = clock

    def boom(*a):
        raise RuntimeError("webhook 挂了")
    poller._source_failed(Src(), sources.SourceBroken("x"), alert=boom)
    tick(31)
    poller._source_failed(Src(), sources.SourceBroken("x"), alert=boom)   # 不抛就算过


def test_主循环里成功和三种失败都接上了健康检查():
    import inspect
    src = inspect.getsource(poller._run_round)
    assert src.count("_source_failed(src, e)") == 3, "限流、改版、其他报错三个分支都要记"
    assert "_source_ok(src)" in src
    assert "except sources.SourceBroken" in src
