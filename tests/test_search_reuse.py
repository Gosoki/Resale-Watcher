"""同关键词的规则共用搜索页。

「RTX 5090 单卡」和「RTX 5090 整机」关键词一模一样，只是价格区间和词表不同 ——
原先每条规则各翻一遍同样的两页，一天约 500 次纯重复请求（2026-09-17 体检估的）。

【锁三个方向】复用多了 quick_min 名存实亡（一条规则复用自己上一轮的页，永远看不到新货）；
复用少了等于没做；翻到一半抛异常的残缺页序列绝不能进缓存（下一条规则的 seen 集会缺一截，
对账立刻把几十件在售商品判成失踪）。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import poller  # noqa: E402

T0 = datetime(2026, 9, 17, 5, 0)


class Src:
    key = "mercari"

    def __init__(self, pages=1, fail_at=None):
        self.calls, self.pages, self.fail_at = [], pages, fail_at

    def search(self, kw, *, sold=False, page_token=""):
        n = len(self.calls) + 1
        self.calls.append((kw, sold, page_token))
        if self.fail_at == n:
            raise RuntimeError("HTTP 403")
        idx = int(page_token or 0) + 1
        return {"items": [], "next": str(idx) if idx < self.pages else "", "total": 0}


def wire(reuse_min, now_box):
    saved = (poller.store.get_settings, config.now, dict(poller._page_cache))
    poller.store.get_settings = lambda force=False: {"search_reuse_min": reuse_min}
    config.now = lambda: now_box[0]
    poller._page_cache.clear()

    def restore():
        poller.store.get_settings, config.now, cache = saved
        poller._page_cache.clear(); poller._page_cache.update(cache)
    return restore


R1 = {"id": 1, "keyword": "RTX 5090", "max_pages": 5}
R4 = {"id": 4, "keyword": "RTX 5090 ", "max_pages": 5}     # 多一个空格，strip 后同键
R2 = {"id": 2, "keyword": "RTX 4090", "max_pages": 5}


def test_另一条规则刚翻过的整批页直接复用():
    src, now = Src(pages=2), [T0]
    restore = wire(5, now)
    try:
        a, _ = poller._pages(src, R1, sold=False)
        now[0] = T0 + timedelta(minutes=3)
        b, _ = poller._pages(src, R4, sold=False)
        assert len(src.calls) == 2, "两页都该来自第一次，整机规则一个请求都不该发"
        assert a is b
    finally:
        restore()


def test_自己上一轮的页绝不复用():
    src, now = Src(), [T0]
    restore = wire(5, now)
    try:
        poller._pages(src, R1, sold=False)
        now[0] = T0 + timedelta(minutes=3)
        poller._pages(src, R1, sold=False)
        assert len(src.calls) == 2
    finally:
        restore()


def test_过期的不复用():
    src, now = Src(), [T0]
    restore = wire(5, now)
    try:
        poller._pages(src, R1, sold=False)
        now[0] = T0 + timedelta(minutes=6)
        poller._pages(src, R4, sold=False)
        assert len(src.calls) == 2
    finally:
        restore()


def test_翻到一半抛异常的不进缓存():
    """残缺的页序列会让下一条规则的 seen 集缺一截，对账立刻把在售商品判成失踪。"""
    src, now = Src(pages=3, fail_at=2), [T0]
    restore = wire(5, now)
    try:
        try:
            poller._pages(src, R1, sold=False)
        except RuntimeError:
            pass
        assert poller._page_cache == {}, "翻到一半的不许缓存"
    finally:
        restore()


def test_不同关键词和成交搜索各是各的():
    src, now = Src(), [T0]
    restore = wire(5, now)
    try:
        poller._pages(src, R1, sold=False)
        poller._pages(src, R2, sold=False)
        poller._pages(src, R4, sold=True)
        assert len(src.calls) == 3
    finally:
        restore()


def test_关闭时一律真发_且不留缓存():
    src, now = Src(), [T0]
    restore = wire(0, now)
    try:
        poller._pages(src, R1, sold=False)
        poller._pages(src, R4, sold=False)
        assert len(src.calls) == 2 and poller._page_cache == {}
    finally:
        restore()


def test_两处搜索都走复用():
    import inspect
    for fn in (poller.scan_on_sale, poller.scan_sold):
        src = inspect.getsource(fn)
        assert "src.search(" not in src, f"{fn.__name__} 绕过了 _pages"
        assert "_pages(src, rule" in src


def test_交易中的按更长的间隔核实():
    """trading 的商品不出现在在售搜索里，每轮都"失踪"；按 20 分钟拉详情一天白烧上百次。"""
    from tests.test_reconcile import FakeStore as _unused  # noqa: F401  只是确认那边的假对象还能 import
    calls = []

    class S:
        def on_sale_items(self, rid, src):
            old = config.now() - timedelta(minutes=60)
            return [{"item_id": "t1", "status": "trading", "last_seen_at": old, "matched": 1},
                    {"item_id": "o1", "status": "on_sale", "last_seen_at": old, "matched": 1}]
        def touch_seen(self, *a): ...
        def update_tracked(self, *a): ...
        def set_status(self, *a, **k): ...
        def add_sold_sample(self, *a): ...
        def refresh_median(self, rid): return (None, 0)
        def get_settings(self, force=False): return {}

    class Src2:
        key = "mercari"
        def can_detail(self, iid): return True
        def detail(self, iid):
            calls.append(iid); return {"status": "on_sale", "price": 1, "bid_count": None, "ship_from": ""}

    saved = poller.store
    poller.store = S()
    try:
        poller.reconcile_sold(Src2(), {"id": 1, "name": "r", "missing_grace_min": 20,
                                       "trading_recheck_min": 180, "detail_budget": 5}, seen=set())
    finally:
        poller.store = saved
    assert calls == ["o1"], f"60 分钟前见过的交易中商品不该按 20 分钟核实，实际核了 {calls}"
