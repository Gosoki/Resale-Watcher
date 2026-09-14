"""图片角上的追踪星 / 标记旗：点一下就地翻面，不重建整页。

【为什么这条要测】它守的是一个【性能】性质，而性能回归不会让任何测试变红，
只会让人觉得"这页越来越卡"然后不再用它。原先每点一次「标记」要跑 1.24 秒的
SQL —— 命中页 697ms（19 条查询）+ 成交页 344ms + 追踪页 137ms + 标记页 64ms，
外加上千个页面元素重新下发，而你看得见的变化只是一面旗换了颜色。

以后但凡有人为了修"别的页上那面旗是旧的"而把 hits_view.refresh() 加回去，
这里会拦下来 —— 正确的修法是 stale_tabs()，切过去时再重建。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import ui as webui  # noqa: E402

ICONS = ("☆", "★")
TIPS = ("去追踪", "别追了")
BASE = "absolute top-0 right-0 star-btn"


def build(twins, on=False, log=None):
    """造一个角标按钮，返回 (按钮, 点它的函数)。"""
    webui.corner_toggle(on, ICONS, TIPS, BASE, "star-on",
                        lambda v: (log if log is not None else []).append(v),
                        twins, ("star", "mercari", "m1"))
    btn = _last_button()
    return btn, _clicker(btn)


def _last_button():
    from nicegui import context
    stack = context.slot.parent.default_slot.children
    return [c for c in stack if c.tag == "q-btn"][-1]


def _clicker(btn):
    """拿到这颗按钮的 click 处理函数 —— 测试里"点一下"就是调它。"""
    handler = [ln.handler for ln in btn._event_listeners.values() if ln.type == "click"][0]
    return lambda: handler(None)


def label(btn):
    return btn._props.get("label")


def test_点一下就地翻面_不动别人():
    log = []
    btn, click = build({}, on=False, log=log)
    assert label(btn) == "☆" and "star-on" not in btn._classes

    click()
    assert label(btn) == "★", "图标没跟着翻"
    assert "star-on" in btn._classes, "高亮样式没加上"
    assert log == [True], "落库的动作没被调用，或状态传错了"

    click()
    assert label(btn) == "☆" and "star-on" not in btn._classes
    assert log == [True, False], "第二次点必须传相反的状态，而不是重复第一次"


def test_同一件商品的两个角标一起翻():
    """【命中页上同一件捡漏会出现两次】捡漏汇总里一次、它所在的规则组里再一次。
    只翻被点的那个，同一屏上就会一颗星实心一颗空心 —— 你会以为自己点漏了，
    再点一次，等于又翻回来，而且你不知道。
    """
    twins, log = {}, []
    a, click_a = build(twins, on=False, log=log)
    b, _ = build(twins, on=False, log=log)

    click_a()
    assert label(a) == "★" and label(b) == "★", "另一个角标没跟着翻"
    assert log == [True], "两个角标一起翻，但落库只该发生一次"


def test_两个角标共用一份状态():
    """点了 A 再点 B，B 必须接着 A 的状态往下翻，而不是从它自己建出来时的状态翻。"""
    twins, log = {}, []
    a, click_a = build(twins, on=False, log=log)
    b, click_b = build(twins, on=False, log=log)

    click_a()                      # 关 -> 开
    click_b()                      # 开 -> 关
    assert log == [True, False]
    assert label(a) == label(b) == "☆"


def test_没给_twins_时各管各的():
    """标记页和成交页用不到 twins（一件商品在那些页上只出现一次）。
    传 None 时不能崩，也不能和别的按钮串在一起。
    """
    a, click_a = build(None, on=False)
    b, _ = build(None, on=True)
    click_a()
    assert label(a) == "★"
    assert label(b) == "★", "另一个按钮是独立的，不该被影响"


def test_翻面必须画在落库之前():
    """【这一步就是为了"马上生效"】落库要走一个数据库往返，画在后面就白做了。"""
    seen = []
    twins = {}
    webui.corner_toggle(False, ICONS, TIPS, BASE, "star-on",
                        lambda v: seen.append(label(_last_button())),
                        twins, ("star", "mercari", "m9"))
    btn = _last_button()
    _clicker(btn)()
    assert seen == ["★"], "落库的时候图标还没翻过来 —— 说明画在了落库之后"


def test_两个开关都不许重建命中页():
    """【这是这次改动的全部意义】命中页重建一次是 19 条 SQL、697ms，
    而点一颗星/一面旗，那一页上真正变的只有这一个角标 —— 它已经就地翻好了。

    别的页（追踪/成交）上那面旗确实会变旧，但正确的修法是 stale_tabs()：
    标成过时，等你切过去时再重建，那一下的开销藏在切页动作里，看不出来。
    """
    import inspect

    for fn in (webui.toggle_track, webui.toggle_mark):
        src = inspect.getsource(fn)
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        assert "hits_view.refresh" not in code, (
            f"{fn.__name__} 又去重建命中页了 —— 点一下要多等 697ms。"
            "要让别的页跟上，用 stale_tabs()")
