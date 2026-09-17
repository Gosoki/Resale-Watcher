"""售出对账的收敛性测试。

【为什么单独给这条路径写测试】它的失败方式是完全静默的：日志里只有几行
「详情读不出状态」，面板上什么都不变，直到详情预算被僵尸吃光、真正卖掉的
商品不再被核实、最后每日配额烧穿导致三个源一起停抓。

这条路径的正确性靠四条互相独立的约定撑着，每一条都是踩出来才加的，
而且改回去之后其余 43 条测试照样全绿：

  1. seen.add 必须在 keep 过滤【之前】—— 否则收窄规则会批量制造僵尸
  2. 核实完无论结果如何都要 touch_seen —— 否则每轮重新核实，永不收敛
  3. detail() 返回的每一个 status 取值都要有对应分支 —— 漏一个就是永久僵尸
  4. DESC_UNREAD 是终态，revalidate 不能把它重算掉

前三条都是同一个 bug 的不同触发路径，已经各自出过一次事故。
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import poller  # noqa: E402

RULE = {
    "id": 1, "name": "测试", "keyword": "RTX 5090", "sources": "",
    "include_all": "5090", "include_any": "", "exclude_any": "ジャンク", "warn_desc": "",
    "price_min": 0, "price_max": 0, "condition_ids": "", "allow_shops": 1,
    "check_desc": 0, "deal_ratio": 90, "quick_min": 7,
    "max_pages": 5, "detail_budget": 10, "missing_grace_min": 20,
    "median_window_days": 30, "median_min_samples": 5, "sold_scan_hours": 24,
}


class FakeStore:
    def update_tracked(self, *a, **k):   # 对账核实到在售时会写回价/出价数
        pass

    """只实现 poller 在这条路径上真正用到的那几个方法。"""

    def refresh_median(self, rid):   # 成交样本进来后会刷中位数
        return (None, 0)

    def get_settings(self, force=False):   # _search 读 search_reuse_min；空字典＝不复用
        return {}

    def __init__(self, items):
        self.items = {i["item_id"]: dict(i) for i in items}
        self.status_calls = []

    def on_sale_items(self, rule_id, source):
        # 真实实现按 last_seen_at 升序，这里照做（第 3 条约定）
        rows = [i for i in self.items.values() if i["status"] in ("on_sale", "trading")]
        return sorted(rows, key=lambda r: r["last_seen_at"])

    def touch_seen(self, source, item_id, rule_id):
        self.items[item_id]["last_seen_at"] = config.now()

    def set_status(self, source, item_id, rule_id, status, sold_at=None, price=None):
        self.items[item_id]["status"] = status
        self.status_calls.append((item_id, status))

    def add_sold_sample(self, *a, **k):
        pass

    def upsert_item(self, rule_id, snap, verdict):
        it = self.items.setdefault(snap["item_id"], dict(snap, status="on_sale"))
        it.update(last_seen_at=config.now(), matched=verdict["matched"])
        return {"new": False, "price_changed": False, "old_price": snap["price"]}

    def update_source_state(self, *a, **k):
        pass


class FakeSource:
    key = "fake"
    name = "测试源"

    def __init__(self, pages, detail_result):
        self.pages = pages
        self.detail_result = detail_result
        self.detail_calls = []

    def search(self, keyword, *, sold=False, page_token=""):
        return {"items": self.pages, "next": "", "total": len(self.pages)}

    def can_detail(self, iid):
        return True

    def detail(self, item_id):
        self.detail_calls.append(item_id)
        return (self.detail_result(item_id) if callable(self.detail_result)
                else self.detail_result)


def _item(iid, *, status="on_sale", seen_ago_min=60, matched=1, name="RTX 5090 美品"):
    return {"item_id": iid, "source": "fake", "name": name, "price": 500000,
            "status": status, "matched": matched, "desc_checked": 0, "desc_warn": "",
            "description": None, "item_type": "user", "condition_id": 3,
            "last_seen_at": config.now() - timedelta(minutes=seen_ago_min)}


def _snap(iid, name="RTX 5090 美品"):
    return {"source": "fake", "item_id": iid, "name": name, "price": 500000,
            "status": "on_sale", "condition_id": 3, "item_type": "user",
            "category_id": None, "brand_name": "", "seller_id": "s1", "thumb_url": "",
            "listed_at": None, "updated_at_src": None,
            "end_time": None, "bid_count": None, "buy_now_price": None}


def _run(monkeypatch, items, pages, detail_result):
    fs, src = FakeStore(items), FakeSource(pages, detail_result)
    monkeypatch.setattr(poller, "store", fs)
    poller.scan_on_sale(src, RULE)
    return fs, src


# ---------------------------------------------------------------- 约定 1

def test_搜到了但规则判它不要的商品_不该被当成失踪去核实(monkeypatch):
    """seen.add 必须在 keep 过滤之前。

    放到之后的话：你一收窄 include_all，那些还挂在库里的老商品就既不更新
    last_seen_at、也不进 seen —— 每轮白烧一个详情请求确认「它还在售」，
    然后什么都不做，下轮重来，永不收敛。改一次词表就能制造出一批。
    """
    fs, src = _run(monkeypatch,
                   items=[_item("m1")],
                   pages=[_snap("m1", "RTX 5090 ジャンク品")],   # 命中排除词 → keep=True 但 matched=0
                   detail_result={"status": "on_sale", "price": 1, "name": "", "description": None})
    assert src.detail_calls == [], "搜索结果里明明有它，不该去核实"


def test_第一层就被丢弃的商品同样不该触发核实(monkeypatch):
    fs, src = _run(monkeypatch,
                   items=[_item("m1")],
                   pages=[_snap("m1", "NVIDIA V100 32GB")],      # 必含词不匹配 → keep=False
                   detail_result={"status": "on_sale", "price": 1, "name": "", "description": None})
    assert src.detail_calls == [], "keep=False 也是「这轮搜索见到过它」"


# ---------------------------------------------------------------- 约定 2

def test_核实完还在售_下一轮不该重复核实(monkeypatch):
    """核实后必须 touch_seen，否则 last_seen_at 永远停在旧值、每轮重来。"""
    fs, src = _run(monkeypatch, items=[_item("m1")], pages=[],
                   detail_result={"status": "on_sale", "price": 1, "name": "", "description": None})
    assert src.detail_calls == ["m1"], "搜不到了，第一轮该核实一次"
    poller.scan_on_sale(src, RULE)                    # 紧接着再跑一轮
    assert src.detail_calls == ["m1"], "刚核实过，不该在宽限期内重复核实"


def test_核实时读不出状态_也不该重复核实(monkeypatch):
    """status='' 是「详情页打开了但解析不出来」。三个分支都不命中，
    但上面已经 touch 过，所以不能变成每轮重来的死循环。"""
    fs, src = _run(monkeypatch, items=[_item("m1")], pages=[],
                   detail_result={"status": "", "price": 0, "name": "", "description": None})
    poller.scan_on_sale(src, RULE)
    assert src.detail_calls == ["m1"]
    assert fs.items["m1"]["status"] == "on_sale", "读不出状态时不该乱改状态"


# ---------------------------------------------------------------- 约定 3

def test_detail_返回的每个状态都要有分支(monkeypatch):
    """漏一个取值就是一个永久僵尸。gone 这一条就是 ヤフオク 的流标拍卖，
    上线时漏掉了：它既不是 sold_out 也不是 trading，落到最后什么都不做，
    而它再也不会出现在在售搜索结果里 —— 每个宽限期都重新核实一次。"""
    for status, expect in [("sold_out", "sold_out"), ("trading", "trading"), ("gone", "gone")]:
        fs, src = _run(monkeypatch, items=[_item("m1")], pages=[],
                       detail_result={"status": status, "price": 500000,
                                      "name": "", "description": None})
        assert fs.items["m1"]["status"] == expect, f"detail 返回 {status} 时状态没跟着变"


def test_商品被删除时标记为下架(monkeypatch):
    fs, src = _run(monkeypatch, items=[_item("m1")], pages=[], detail_result=None)
    assert fs.items["m1"]["status"] == "gone"


# ---------------------------------------------------------------- 预算与轮转

def test_核实预算用完后轮转到没核实过的那批(monkeypatch):
    """待核实的比预算多时，必须按 last_seen_at 升序轮转。
    没有排序的话同一批商品每轮都排在前面吃光预算，真正卖掉的永远轮不到。"""
    rule = dict(RULE, detail_budget=2)
    items = [_item(f"m{i}", seen_ago_min=100 - i) for i in range(5)]
    fs, src = FakeStore(items), FakeSource([], {"status": "on_sale", "price": 1,
                                                "name": "", "description": None})
    monkeypatch.setattr(poller, "store", fs)
    poller.scan_on_sale(src, rule)
    first = list(src.detail_calls)
    assert len(first) == 2, "一轮最多花 detail_budget 个请求"
    poller.scan_on_sale(src, rule)
    second = src.detail_calls[2:]
    assert set(first) & set(second) == set(), "第二轮该轮到别的商品，不能卡在同一批"


def test_没扫全时整个跳过售出对账(monkeypatch):
    """被 max_pages 截断时，「没出现在搜索结果里」不等于「卖掉了」。"""
    rule = dict(RULE, max_pages=1)
    fs = FakeStore([_item("m1")])
    src = FakeSource([], {"status": "sold_out", "price": 1, "name": "", "description": None})
    src.search = lambda *a, **k: {"items": [], "next": "还有下一页", "total": 999}
    monkeypatch.setattr(poller, "store", fs)
    poller.scan_on_sale(src, rule)
    assert src.detail_calls == [], "没扫全就不该做对账"
