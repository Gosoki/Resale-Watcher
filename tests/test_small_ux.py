"""几处小的：规则保存后立刻重判、关键词不匹配的原因、有备注的标记不许顺手删、全部页搜索。"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.matcher import explain  # noqa: E402
from web import ui as webui  # noqa: E402


def test_关键词不匹配有具体原因():
    out = explain({"keyword": "RTX 5090"}, {"reject_reason": "no_keyword", "price": 1})
    assert "RTX 5090" in out and out.strip()


def test_规则保存后立刻重判和重算捡漏():
    src = inspect.getsource(webui.rule_dialog)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    i = code.index("store.update_rule(rule[\"id\"], payload)")
    tail = code[i:i + 400]
    assert "poller.revalidate(" in tail and "poller.mark_deals(" in tail, "保存后不重判会刷出「新摘要 + 旧徽标」"


def test_有备注的标记取消时拒绝并返回False():
    from db import store

    class S:
        called = []
        def mark_note_of(self, so, ii): return "别忘了这件"
        def set_marked(self, *a): S.called.append(a)
        def marked_ids(self): return set()
    saved_store, saved_notify, saved_refresh = webui.store, webui.notify, webui.marks_view.refresh
    webui.store, webui.notify = S(), lambda *a, **k: None
    webui.marks_view.refresh = lambda *a, **k: None       # 没有事件循环，refresh 会炸
    try:
        r = webui.toggle_mark({"source": "mercari", "item_id": "m1", "name": "x", "price": 1}, "R", False)
        assert r is False and S.called == [], "有备注的不许删"
        r2 = webui.toggle_mark({"source": "mercari", "item_id": "m1", "name": "x", "price": 1, "note": ""}, "R", False)
        assert r2 is None and len(S.called) == 1, "没备注的照常删"
    finally:
        webui.store, webui.notify = saved_store, saved_notify
        webui.marks_view.refresh = saved_refresh


def test_全部页能按标题和卖家搜():
    src = inspect.getsource(webui.all_view.func if hasattr(webui.all_view, "func") else webui.all_view)
    assert "name LIKE %s OR seller_id LIKE %s" in src
    create = inspect.getsource(webui.create)
    assert 'placeholder="搜标题 / 卖家ID"' in create
