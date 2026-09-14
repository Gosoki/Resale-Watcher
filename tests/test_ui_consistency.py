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
