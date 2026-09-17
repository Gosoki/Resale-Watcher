"""卖掉的商品怎么才能被标成卖掉 —— 三个源三种坑，都在 2026-09-17 的体检里撞出来的。

  メルカリ   已删除的商品详情回 403+InvisibleItemException（见 test_mercari_deleted.py）
  フリマ     已售出的商品页没有 __NEXT_DATA__，只剩 schema.org 的 Product 块
  两个源     对账靠逐件拉详情，预算每轮 10 件；僵尸把预算吃光，真卖掉的永远轮不到 ——
            库里 273 件 メルカリ「在售」里成交的只标出来 1 件

这里锁三件事：
  A. フリマ 的 detail() 在没有 __NEXT_DATA__ 时退到 schema.org，能读出 sold_out 和价格
  B. 对账读到"卖掉了"但读不到价格时，不许把 ¥0 写进成交样本（中位数是捡漏线的全部依据）
  C. 成交轮搜到的已售出商品，库里还标在售的直接改状态 —— 一个请求都不多发
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import poller  # noqa: E402
from sources import yahoo_flea  # noqa: E402

T_NOW = datetime(2026, 9, 17, 5, 0)

SOLD_PAGE = '''<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",
"name":"ASUS ROG Matrix Platinum GeForce RTX 5090 OC","image":["x.jpg"],"url":"https://x",
"description":"新品未使用",
"offers":{"@type":"Offer","priceCurrency":"JPY","price":1280000,
"itemCondition":"https://schema.org/NewCondition","availability":"https://schema.org/OutOfStock"}}</script>
</head><body>SOLD</body></html>'''

OPEN_PAGE = SOLD_PAGE.replace("OutOfStock", "InStock")


# ---------------------------------------------------------------- A

def test_没有NEXT_DATA时从schema_org读出已售出():
    d = yahoo_flea._from_ld_json(SOLD_PAGE)
    assert d["status"] == "sold_out"
    assert d["price"] == 1280000
    assert d["name"].startswith("ASUS ROG Matrix")


def test_schema_org的InStock是在售():
    assert yahoo_flea._from_ld_json(OPEN_PAGE)["status"] == "on_sale"


def test_两种数据都没有时仍是读不出的哨兵_不是None():
    """None 是"商品没了"的语义，会被上层标成下架 —— 页面结构变了不等于商品没了。"""
    assert yahoo_flea._from_ld_json("<html>nothing</html>") is None


def test_detail在没有NEXT_DATA时会走schema_org():
    class Resp:
        status_code = 200
        text = SOLD_PAGE

    src = yahoo_flea.YahooFlea.__new__(yahoo_flea.YahooFlea)
    src._call = lambda *a, **k: Resp()
    d = src.detail("z677504374")
    assert d["status"] == "sold_out" and d["price"] == 1280000


# ---------------------------------------------------------------- B

class FakeStore:
    def refresh_median(self, rid):   # 成交样本进来后会刷中位数
        return (None, 0)

    def get_settings(self, force=False):   # _search 读 search_reuse_min；空字典＝不复用
        return {}

    def __init__(self, items):
        self.items = items
        self.samples, self.statuses, self.touched = [], [], []

    def on_sale_items(self, rid, src):
        return self.items

    def touch_seen(self, *k):
        self.touched.append(k)

    def set_status(self, source, iid, rid, status, sold_at=None, price=None):
        self.statuses.append((iid, status) if price is None else (iid, status, price))

    def add_sold_sample(self, rid, source, iid, price, sold_at, kind):
        self.samples.append((iid, price))


class FakeSrc:
    key = "yahoo_flea"

    def __init__(self, detail):
        self._detail = detail

    def can_detail(self, iid):
        return True

    def detail(self, iid):
        return self._detail


def run_reconcile(detail, matched=1):
    old = config.now() - timedelta(hours=2)
    fake = FakeStore([{"item_id": "z1", "last_seen_at": old, "matched": matched}])
    saved = poller.store
    poller.store = fake
    try:
        poller.reconcile_sold(FakeSrc(detail),
                              {"id": 1, "name": "r", "missing_grace_min": 20, "detail_budget": 5},
                              seen=set())
    finally:
        poller.store = saved
    return fake


def test_读到卖掉了但价格是0时不进样本():
    """状态和价格来自两个不同的解析路径，能读到"卖掉了"不代表能读到"多少钱"。
    0 进了 sold_sample 会把中位数往下拽。"""
    fake = run_reconcile({"status": "sold_out", "price": 0, "description": None})
    assert fake.statuses == [("z1", "sold_out", 0)], "状态照改（价 0 由 set_status 自己忽略）"
    assert fake.samples == [], "¥0 不许进样本"


def test_价格正常时照常进样本_并写回商品():
    """【成交价要写回 item】不写回的话成交页上的价格/百分比全是它在售时的旧值。"""
    fake = run_reconcile({"status": "sold_out", "price": 890000, "description": ""})
    assert fake.samples == [("z1", 890000)]
    assert fake.statuses == [("z1", "sold_out", 890000)]


def test_set_status带价时写价_价为0时不动价():
    from db import store

    def capture(fn, *a, **kw):                     # 截 store.execute 真正发出去的 (SQL, 参数)
        got = {}
        saved = store.execute
        store.execute = lambda sql, params=None: got.update(sql=sql, params=params)
        try:
            fn(*a, **kw)
        finally:
            store.execute = saved
        return got["sql"], got["params"]

    sql, params = capture(store.set_status, "s", "i", 1, "sold_out", sold_at=T_NOW, price=890000)
    assert "price = %s" in sql and 890000 in params
    sql0, _ = capture(store.set_status, "s", "i", 1, "sold_out", sold_at=T_NOW, price=0)
    assert "price = %s" not in sql0, "价读不到（0）时不许把 0 写进去"


# ---------------------------------------------------------------- C

class SoldScanStore(FakeStore):
    def get_settings(self, force=False):   # _search 读 search_reuse_min；空字典＝不复用
        return {}

    def __init__(self, live_ids):
        super().__init__([])
        self.live = set(live_ids)

    def update_source_state(self, *a, **k):
        pass

    def prune_sold_samples(self, rid):
        pass

    def refresh_median(self, rid):
        return None, 0

    def live_ids_among(self, rid, source, ids):
        return [i for i in ids if i in self.live]


class SoldSrc:
    key, name = "mercari", "メルカリ"

    def __init__(self, items):
        self.items = items

    def search(self, kw, *, sold=False, page_token=""):
        return {"items": self.items, "next": "", "total": len(self.items)}


def snap(iid, price, when):
    return {"item_id": iid, "price": price, "updated_at_src": when, "name": iid}


def test_成交搜索里看到的_库里还在售的_直接改成售出():
    """【一个请求都不多发】这些页本来就要翻。原先只能靠对账逐件拉详情，
    僵尸把预算吃光，真卖掉的永远轮不到 —— 273 件里只标出来 1 件。"""
    when = config.now() - timedelta(hours=1)
    fake = SoldScanStore(live_ids={"m1", "m3"})
    src = SoldSrc([snap("m1", 500_000, when), snap("m2", 480_000, when), snap("m3", 1, when)])
    saved_store, saved_judge = poller.store, poller.judge_snap
    poller.store = fake
    poller.judge_snap = lambda rule, s: {"matched": s["price"] > 10}     # m3 是废品，不进样本
    try:
        poller.scan_sold(src, {"id": 1, "name": "r", "keyword": "RTX", "max_pages": 1,
                               "median_window_days": 30, "median_min_samples": 5, "price_max": 0})
    finally:
        poller.store, poller.judge_snap = saved_store, saved_judge
    assert sorted(fake.statuses) == [("m1", "sold_out"), ("m3", "sold_out")], \
        "库里在售、平台说卖了的两件都该改状态 —— 包括没过价格规则的 m3"
    assert fake.samples == [("m1", 500_000), ("m2", 480_000)], \
        "样本仍只收过了规则的（m3 是废品不进）；m2 不在库里，只进样本、不改状态"


def test_成交搜索一件都没命中库里的时候不发多余查询():
    fake = SoldScanStore(live_ids=set())
    calls = []
    fake.live_ids_among = lambda rid, source, ids: calls.append(ids) or []
    saved_store, saved_judge = poller.store, poller.judge_snap
    poller.store = fake
    poller.judge_snap = lambda rule, s: {"matched": False}
    try:
        poller.scan_sold(SoldSrc([]), {"id": 1, "name": "r", "keyword": "RTX", "max_pages": 1,
                                       "median_window_days": 30, "median_min_samples": 5, "price_max": 0})
    finally:
        poller.store, poller.judge_snap = saved_store, saved_judge
    assert calls == [], "搜索结果为空时不该去查库"
