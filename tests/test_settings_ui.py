"""设置页的分组表。

【为什么这一条必须有测试】面板是按 SETTING_GROUPS 渲染的，不是按 SETTINGS_SPEC。
加了新设置项却忘了往分组里写，它就【在面板上整个消失】—— 而库里的值照常生效、
轮询照常读它。表现是"我记得有这个设置，怎么找不到了"，翻半天代码才知道原因。
反过来，分组里写了个不存在的键会直接 KeyError 把整页打崩。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

GROUPED = {k: g for g, ks in config.SETTING_GROUPS.items() for k, _ in ks}


def test_每个设置项都要出现在面板上():
    missing = set(config.SETTINGS_SPEC) - set(GROUPED)
    assert not missing, f"这些设置项没分组，面板上看不见：{sorted(missing)}"


def test_分组里不许有不存在的键():
    ghost = set(GROUPED) - set(config.SETTINGS_SPEC)
    assert not ghost, f"这些键不在 SETTINGS_SPEC 里，渲染时会 KeyError：{sorted(ghost)}"


def test_一个设置项只能归一个组():
    seen = [k for ks in config.SETTING_GROUPS.values() for k, _ in ks]
    dup = {k for k in seen if seen.count(k) > 1}
    assert not dup, f"重复分组会在面板上出现两个输入框，保存时互相覆盖：{sorted(dup)}"


def test_中文名不能留空也不能就是英文键():
    bad = [k for k, label in
           ((k, lb) for ks in config.SETTING_GROUPS.values() for k, lb in ks)
           if not label.strip() or label.strip() == k]
    assert not bad, f"这些项没写中文名：{bad}"
