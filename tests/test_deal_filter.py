"""命中页「捡漏」汇总的筛选条：按规则、按平台多选。

不连库：直接拿假的行和外框渲染筛选条，点标签、看外框的显示有没有跟着变。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import ui as wui  # noqa: E402

BY_ID = {1: {"id": 1, "name": "RTX 5090 单卡"}, 2: {"id": 2, "name": "RTX 4090 单卡"},
         6: {"id": 6, "name": "RTX 4080 / 4080 SUPER 单卡"}}


def test_同组是或_两组是且_不选等于不筛():
    sel = {"rule": set(), "source": set()}
    assert wui.deal_visible(1, "mercari", sel)
    sel = {"rule": {1, 2}, "source": set()}
    assert wui.deal_visible(2, "yahoo_flea", sel) and not wui.deal_visible(6, "mercari", sel)
    sel = {"rule": {1, 2}, "source": {"mercari"}}
    assert wui.deal_visible(1, "mercari", sel)
    assert not wui.deal_visible(1, "yahoo_flea", sel), "两组之间是「且」"


@pytest.fixture
def bar(monkeypatch):
    """渲染一条筛选条，返回 (外框列表, 按文字索引的标签, 状态文字元素, 清除按钮)。"""
    from nicegui import Client, ui
    from nicegui.page import page as _page

    monkeypatch.setattr(wui, "_DEAL_FILTER", {"rule": set(), "source": set()})
    ctx = Client(_page("/_t_deal_filter"))

    def render(rows):
        with ctx:
            host = ui.element("div")
            boxes = []
            for r in rows:
                boxes.append((ui.element("div"), r["rule_id"], r["source"]))
            with host:
                wui._deal_filter_bar(rows, BY_ID, boxes)
            chips = {c.text: c for c in host.descendants() if isinstance(c, ui.chip)}
            labels = [e for e in host.descendants() if type(e) is ui.label]
            buttons = [e for e in host.descendants() if isinstance(e, ui.button)]
            status = labels[-1] if labels else None
            return boxes, chips, status, (buttons[0] if buttons else None)
    return render, ctx


ROWS = [{"rule_id": 1, "source": "mercari"}, {"rule_id": 1, "source": "yahoo_flea"},
        {"rule_id": 2, "source": "mercari"}, {"rule_id": 6, "source": "yahoo_auction"}]


def _click(el):
    """模拟浏览器点一下：走和前端事件同一个入口（Element._handle_event）。"""
    for lid, lst in el._event_listeners.items():
        if lst.type == "click":
            el._handle_event({"listener_id": lid, "args": {}})


def _shown(boxes):
    return [(rid, src) for box, rid, src in boxes if box.visible]


def test_点规则标签只留下那条规则的_再点平台取交集_清除后全部回来(bar):
    render, ctx = bar
    boxes, chips, status, clear = render(ROWS)
    with ctx:
        # 标签带件数，按规则页 / 数据源注册的顺序排
        assert list(chips) == ["RTX 5090 单卡 2", "RTX 4090 单卡 1", "RTX 4080 / 4080 SUPER 单卡 1",
                               "メルカリ 2", "Yahoo!フリマ 1", "ヤフオク 1"]
        assert len(_shown(boxes)) == 4 and status.text == "" and not clear.visible

        chips["RTX 5090 单卡 2"].set_selected(True)
        assert _shown(boxes) == [(1, "mercari"), (1, "yahoo_flea")]
        assert status.text == "显示 2 / 4 件" and clear.visible

        chips["RTX 4090 单卡 1"].set_selected(True)                  # 同组多选：或
        assert len(_shown(boxes)) == 3

        chips["メルカリ 2"].set_selected(True)                        # 跨组：且
        assert _shown(boxes) == [(1, "mercari"), (2, "mercari")]

        _click(clear)                                                 # 「清除筛选」
        assert len(_shown(boxes)) == 4 and status.text == "" and not clear.visible
        assert not any(c.selected for c in chips.values()), "标签也要一起取消选中，不然看着还在筛"


def test_选择在整页重建后保留(bar):
    render, ctx = bar
    _, chips, _, _ = render(ROWS)
    with ctx:
        chips["ヤフオク 1"].set_selected(True)
    boxes, chips, status, _ = render(ROWS)                          # hits_view.refresh() 之后
    with ctx:
        assert chips["ヤフオク 1"].selected
        assert _shown(boxes) == [(6, "yahoo_auction")] and status.text == "显示 1 / 4 件"


def test_选过但已经没有捡漏的那项_不再暗中生效(bar):
    render, ctx = bar
    _, chips, _, _ = render(ROWS)
    with ctx:
        chips["RTX 4080 / 4080 SUPER 单卡 1"].set_selected(True)
    # 下一次重建时规则 6 的那件已经不是捡漏了：它的标签没了，筛选也不能还把别的全藏起来
    boxes, chips, _, _ = render(ROWS[:3])
    with ctx:
        assert "RTX 4080 / 4080 SUPER 单卡 1" not in chips
        assert len(_shown(boxes)) == 3


def test_只有一个选项的组不出现_两组都只有一个就整条不出现(bar):
    render, _ = bar
    _, chips, _, _ = render([{"rule_id": 1, "source": "mercari"}, {"rule_id": 2, "source": "mercari"}])
    assert list(chips) == ["RTX 5090 单卡 1", "RTX 4090 单卡 1"], "只有一个平台时平台组点了也没用"
    boxes, chips, status, clear = render([{"rule_id": 1, "source": "mercari"}])
    assert chips == {} and status is None and clear is None
    assert all(box.visible for box, _, _ in boxes)
