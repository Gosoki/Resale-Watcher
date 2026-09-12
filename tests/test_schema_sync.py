"""schema.sql → 已有库 的同步逻辑。

【为什么值得单测】这是唯一一处会自动对生产表执行 ALTER 的代码。
它算错了有两种后果：该补的列没补（代码 SELECT 时报 Unknown column），
或者动了不该动的列。后者里最坏的是 DROP —— 那是不可逆地删数据，
所以「只增不删」这条用例是这个文件存在的主要理由。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.store import plan_schema_changes  # noqa: E402

DEF_A = "aaa VARCHAR(8) NOT NULL DEFAULT '' COMMENT '甲'"
DEF_B = "bbb INT NULL COMMENT '乙'"


def test_库里缺的列会被补上():
    adds, mods = plan_schema_changes(
        want={("t", "aaa"): DEF_A, ("t", "bbb"): DEF_B},
        have={("t", "aaa"): "甲"})
    assert adds == [("t", "bbb", DEF_B)]
    assert mods == []


def test_注释变了的列会被改():
    adds, mods = plan_schema_changes(
        want={("t", "aaa"): DEF_A},
        have={("t", "aaa"): "旧的说明"})
    assert adds == []
    assert mods == [("t", "aaa", DEF_A)]


def test_没变化的列一个都不碰():
    assert plan_schema_changes(want={("t", "aaa"): DEF_A},
                               have={("t", "aaa"): "甲"}) == ([], [])


def test_库里有而_schema_里没有的列绝不产出_DROP():
    """只增不删。自动删列＝不可逆地删数据，宁可留一列没人用的垃圾。"""
    adds, mods = plan_schema_changes(
        want={("t", "aaa"): DEF_A},
        have={("t", "aaa"): "甲", ("t", "老列"): "以前留下的"})
    assert (adds, mods) == ([], [])


def test_表还没建的时候不补列():
    """表不存在时 CREATE TABLE 会连列带注释一起建出来，这里插手只会报错。"""
    assert plan_schema_changes(want={("新表", "aaa"): DEF_A}, have={("t", "aaa"): "甲"}) == ([], [])


def test_注释里的转义单引号能正确比对():
    """schema.sql 里写 '' 表示一个单引号，库里存的是单个 ' —— 不还原就会
    每次启动都以为注释变了，无谓地 ALTER 一遍全表。"""
    d = "ccc INT NULL COMMENT '这是 '' 引号'"
    assert plan_schema_changes(want={("t", "ccc"): d}, have={("t", "ccc"): "这是 ' 引号"}) == ([], [])
