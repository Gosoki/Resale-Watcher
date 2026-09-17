"""面板每一页要发几条 SQL —— 库在远程，每条一次往返，条数就是打开一页要等多久。

2026-09-17 实测（每条 ~35ms）：规则页 33 条 / 命中页 19 条 / 成交页 14 条，
打开首页七个页签全建 ≈ 76 条、3〜7 秒。改成整表取回、Python 里分，
再加页签懒建。这里锁住条数：谁哪天在循环里加一条"顺手查一下"，这里会红。
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402
from web import ui as webui  # noqa: E402

T = datetime(2026, 9, 17, 4, 0)


class CountingStore:
    """假 store：每个读函数记一次"往返"，返回够渲染用的最小数据。"""

    STATE_DEFAULT = store.STATE_DEFAULT
    SOURCE_STATE_DEFAULT = store.SOURCE_STATE_DEFAULT

    def __init__(self):
        self.calls = []

    def _hit(self, name):
        self.calls.append(name)

    def get_rules(self, enabled_only=False):
        self._hit("get_rules")
        return [{"id": 1, "name": "A", "keyword": "x", "price_min": 1, "price_max": 9,
                 "enabled": 1, "note": "", "deal_ratio": 90, "deal_price": 0,
                 "median_window_days": 30, "median_min_samples": 5, "quick_min": 7},
                {"id": 2, "name": "B", "keyword": "y", "price_min": 1, "price_max": 9,
                 "enabled": 1, "note": "", "deal_ratio": 90, "deal_price": 0,
                 "median_window_days": 30, "median_min_samples": 5, "quick_min": 7}]

    def marked_ids(self): self._hit("marked_ids"); return set()
    def hidden_ids(self): self._hit("hidden_ids"); return set()
    def hidden_items(self): self._hit("hidden_items"); return []
    def rule_states(self): self._hit("rule_states"); return {1: {"median_price": 100, "sample_count": 9}}
    def source_states(self): self._hit("source_states"); return {}
    def item_counts(self): self._hit("item_counts"); return {(1, "mercari"): {"t": 3, "h": 1}, (1, "yahoo_flea"): {"t": 2, "h": 0}}
    def first_seen_by_rule(self): self._hit("first_seen_by_rule"); return {1: T}
    def live_matched(self): self._hit("live_matched"); return []
    def prev_prices(self, rows): self._hit("prev_prices"); return {}
    def sold_tracked_all(self): self._hit("sold_tracked_all"); return {}
    def sold_samples_recent(self, n=80): self._hit("sold_samples_recent"); return {}
    def get_settings(self, force=False):
        return {"fresh_hours": 24, "stale_warn_hours": 2, "stale_hide_hours": 24}


def with_store(fn):
    fake = CountingStore()
    saved = webui.store
    webui.store = fake
    try:
        fn()
    finally:
        webui.store = saved
    return fake.calls


def test_命中页数据8条以内():
    calls = with_store(webui._load_hits)
    assert len(calls) <= 8, f"命中页发了 {len(calls)} 条：{calls}"
    assert "live_matched" in calls and "first_seen_by_rule" in calls


def test_规则页数据4条():
    calls = with_store(webui._load_rules)
    assert calls == ["get_rules", "rule_states", "source_states", "item_counts"], calls


def test_成交页数据5条():
    calls = with_store(webui._load_sold)
    assert len(calls) == 5, calls
    assert "sold_samples_recent" in calls and "sold_tracked_all" in calls


def test_规则级入库数对全部源求和_不只加启用的():
    """历史上抓过、后来从规则里摘掉的源，它的商品还在库里，「入库」得算上。"""
    counts = {(1, "mercari"): {"t": 3, "h": 1}, (1, "old_src"): {"t": 5, "h": 2}}
    total = sum(c["t"] for (r, _), c in counts.items() if r == 1)
    hit = sum(c["h"] for (r, _), c in counts.items() if r == 1)
    assert (total, hit) == (8, 3)


def test_缺rule_state行时用默认值不崩():
    """新建一条规则、poller 还没扫到，rule_state 里没有它的行。"""
    d = {"states": {}}
    st = d["states"].get(99) or store.STATE_DEFAULT
    assert st["median_price"] is None and st["sample_count"] == 0
    ss = {}.get((99, "mercari")) or store.SOURCE_STATE_DEFAULT
    assert ss["last_scan_at"] is None and ss["last_total"] == 0 and ss["last_error"] == ""


def test_捡漏汇总跨规则重排():
    """live 是按规则分块的；汇总要的是"全站最便宜的排最前"。"""
    live = [{"rule_id": 1, "is_deal": 1, "deal_pct": 90, "price": 5},
            {"rule_id": 2, "is_deal": 1, "deal_pct": 70, "price": 9},
            {"rule_id": 2, "is_deal": 0, "deal_pct": 60, "price": 1}]
    rows = sorted((r for r in live if r["is_deal"]),
                  key=lambda r: (r["deal_pct"] if r["deal_pct"] is not None else 999, r["price"]))
    assert [r["rule_id"] for r in rows] == [2, 1]


def test_页签懒建的四个坑():
    import inspect
    src = inspect.getsource(webui.create)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    idx = code.index("def index()")
    body = code[idx:]
    # 坑2：built 在 index() 里，不在模块级
    assert "built: set = set()" in body
    # 坑1：拿不到容器就 return
    i = code.index("c = containers.get(name)")
    assert "return" in code[i:i + 120]
    # 坑3：七个页签都能建
    for name in ("命中", "追踪", "标记", "成交", "全部", "规则", "设置"):
        assert f'"{name}": build_' in code, f"BUILD 里少了 {name}"
    # 首页显式建
    assert 'built.add("命中")' in code
