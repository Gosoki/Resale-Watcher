"""面板的尺寸一致性。

【为什么这一条要写成测试】按钮大小不一致是最容易复发的那类问题：
加一个新按钮时顺手抄了一句带 size=sm 的 props，页面上就多出一档尺寸，
而且不报错、不影响功能，只有把两个按钮摆在一起看才发现 —— 而它们往往
不在同一屏。实际发生过一次：命中页「捡漏价」那一行里，框内的「保存」是
默认尺寸，紧挨着的「设置规则」带着 size=sm，两个并排差一档。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

UI = Path(__file__).resolve().parent.parent / "web" / "ui.py"
# 去掉注释行再检查：注释里会写「原先那个 size=sm 的按钮」这种话
CODE = "\n".join(ln for ln in UI.read_text().splitlines()
                 if not ln.lstrip().startswith("#"))


def test_按钮只有一个尺寸():
    """层级靠颜色和填充区分（BTN_PRIMARY/GHOST/QUIET/DANGER），不靠大小。"""
    bad = re.findall(r"size=(?:xs|sm|lg|xl)", CODE)
    assert not bad, (
        f"有按钮自带尺寸：{sorted(set(bad))} —— 它和别处的按钮并排时会差一档。"
        "要区分层级请改用 BTN_* 常量里的颜色")


def test_按钮的_props_都走常量():
    """手抄一遍 props 字符串，改设计系统时就会漏掉它。"""
    from web import ui  # noqa: PLC0415

    consts = {ui.BTN_PRIMARY, ui.BTN_GHOST, ui.BTN_QUIET,
              ui.BTN_DANGER, ui.BTN_DANGER_SOLID, ui.BTN_CORNER}
    for lit in re.findall(r'\.props\("([^"]*)"\)', CODE):
        if "flat" in lit or "unelevated" in lit:
            assert lit in consts, (
                f'手写了一串按钮 props：{lit!r}。请改用 BTN_* 常量 —— '
                "散落的字面量会在下次统一设计时被漏掉")


def test_同一个动作只能有一个叫法():
    """「保存」和「存」混用过：同一个动作两个名字，人会以为它们不一样。"""
    assert 'ui.button("存"' not in CODE, "保存按钮请统一写「保存」"


def test_每个被_refresh_的视图都真的是_refreshable():
    """装饰器挂错函数 = 整页打不开，而且只在日志里报。

    2026-09-17 实际发生：`@ui.refreshable` 从 `sold_view` 挪到了取数的 `_load_sold`
    上。取数函数不画界面，refresh() 它只会重跑 SQL 再把结果丢掉；而 `sold_view`
    没了 `.refresh` 属性，`build_sold` 里那个「刷新」按钮在【建页签时】就要读它 ——
    切到成交页当场 AttributeError，页签建到一半中断，用户看到的是一片空白。
    规则页同一处错误，表现是「点保存没反应」。

    这条测试静态扫出所有 `X.refresh`，要求 X 带 `@ui.refreshable`。
    """
    import ast  # noqa: PLC0415

    tree = ast.parse(UI.read_text())
    funcs, refreshable = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.add(node.name)
            if any(ast.unparse(d) == "ui.refreshable" for d in node.decorator_list):
                refreshable.add(node.name)

    bad = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == "refresh"
                and isinstance(node.value, ast.Name)
                and node.value.id in funcs and node.value.id not in refreshable):
            bad.setdefault(node.value.id, []).append(node.lineno)
    assert not bad, (
        "这些函数被当成 refreshable 用了，但没带 @ui.refreshable —— "
        f"每一处都会抛 AttributeError：{ {k: sorted(v) for k, v in bad.items()} }")


def test_取数函数不该是_refreshable():
    """`_load_*` 只返回数据、不画任何元素。

    对它 refresh() 是个静默的空操作（重跑一遍 SQL，返回值没人接），
    而把装饰器放在这儿，往往意味着它是从对应的 `*_view` 上挪过来的 —— 那边一挪走就炸。
    """
    import ast  # noqa: PLC0415

    tree = ast.parse(UI.read_text())
    bad = [n.name for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name.startswith("_load_")
           and any(ast.unparse(d) == "ui.refreshable" for d in n.decorator_list)]
    assert not bad, f"取数函数带了 @ui.refreshable：{bad} —— 装饰器应该在画界面的那个 *_view 上"


def test_次操作按钮的字色规则不许波及圆形图标按钮():
    """【真事】2026-09-23 为了让 BTN_GHOST 的蓝字过 AA，在 @layer overrides 里加了
    .q-btn--flat.text-primary{color:亮蓝!important}。缩略图角上的 ★ ⚑ 也是 flat、
    也被 Quasar 挂了 text-primary，而这条的特异性（0,3,0）高过 .star-on/.mark-on（0,2,0）——
    已追踪的琥珀星、已标记的青旗全被刷成蓝色，缩略图上分不出追没追、标没标。
    单元测试里没有浏览器，只能锁住"这条规则必须排除 round"这个写法本身。
    """
    import re
    from web.ui import DARK_CSS
    rules = [r for r in re.findall(r"[^{}]+\{[^{}]*\}", DARK_CSS)
             if ".q-btn--flat.text-primary" in r]
    assert rules, "找不到次操作按钮的字色规则 —— 被删了还是改名了？"
    for r in rules:
        sel = r.split("{")[0]
        for part in sel.split(","):
            if ".q-btn--flat.text-primary" in part:
                assert ":not(.q-btn--round)" in part, (
                    f"这个选择器会刷到 ★ ⚑ ☰ 上：{part.strip()!r}")
