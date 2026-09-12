"""schema.sql → 已有库 的同步逻辑。

【为什么值得单测】这是唯一一处会自动对生产表执行 ALTER 的代码。
它算错了有两种后果：该改的没改（加了列 SELECT 报 Unknown column、
加宽了列继续截断数据），或者动了不该动的列。后者里最坏的是 DROP ——
那是不可逆地删数据，所以「只增不删」这条用例是这个文件存在的主要理由。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.store import plan_schema_changes  # noqa: E402

DEF_A = "aaa VARCHAR(8) NOT NULL DEFAULT '' COMMENT '甲'"
DEF_B = "bbb INT NULL COMMENT '乙'"


def col(comment, typ):
    """库里一列的现状（information_schema 查出来的样子）。"""
    return {"comment": comment, "type": typ}


HAVE_A = {("t", "aaa"): col("甲", "varchar(8)")}


def test_库里缺的列会被补上():
    adds, mods = plan_schema_changes(want={("t", "aaa"): DEF_A, ("t", "bbb"): DEF_B},
                                     have=HAVE_A)
    assert adds == [("t", "bbb", DEF_B)]
    assert mods == []


def test_注释变了的列会被改():
    adds, mods = plan_schema_changes(want={("t", "aaa"): DEF_A},
                                     have={("t", "aaa"): col("旧的说明", "varchar(8)")})
    assert adds == []
    assert mods == [("t", "aaa", DEF_A)]


def test_只改类型不改注释也要同步过去():
    """【这条是踩出来的】把 seller_id 从 VARCHAR(24) 加宽到 VARCHAR(32) 时注释没动，
    只比注释的旧实现会认为「没变化」，加宽对已有的库完全不生效 ——
    而 ヤフオク 的卖家ID 实测 28~29 字符，继续被截断，卖家黑名单里手工填
    从网页复制来的完整 ID 就永远匹配不上。"""
    wide = "aaa VARCHAR(32) NOT NULL DEFAULT '' COMMENT '甲'"
    adds, mods = plan_schema_changes(want={("t", "aaa"): wide}, have=HAVE_A)
    assert adds == []
    assert mods == [("t", "aaa", wide)]


def test_类型和注释都没变时一列都不碰():
    """比类型是为了不漏改，但不能因此每次启动都去 ALTER 一遍全表。
    实测本库 65 个列的 DDL 类型串和 information_schema 的 COLUMN_TYPE 逐一相等
    （TINYINT(1) 这类也对得上），所以直接比字符串不会误报。"""
    assert plan_schema_changes(want={("t", "aaa"): DEF_A}, have=HAVE_A) == ([], [])
    assert plan_schema_changes(want={("t", "bbb"): DEF_B},
                               have={("t", "bbb"): col("乙", "int")}) == ([], [])


def test_库里有而_schema_里没有的列绝不产出_DROP():
    """只增不删。自动删列＝不可逆地删数据，宁可留一列没人用的垃圾。"""
    adds, mods = plan_schema_changes(
        want={("t", "aaa"): DEF_A},
        have={**HAVE_A, ("t", "老列"): col("以前留下的", "int")})
    assert (adds, mods) == ([], [])


def test_表还没建的时候不补列():
    """表不存在时 CREATE TABLE 会连列带注释一起建出来，这里插手只会报错。"""
    assert plan_schema_changes(want={("新表", "aaa"): DEF_A}, have=HAVE_A) == ([], [])


def test_注释里的转义单引号能正确比对():
    """schema.sql 里写 '' 表示一个单引号，库里存的是单个 ' —— 不还原就会
    每次启动都以为注释变了，无谓地 ALTER 一遍全表。"""
    d = "ccc INT NULL COMMENT '这是 '' 引号'"
    assert plan_schema_changes(want={("t", "ccc"): d},
                               have={("t", "ccc"): col("这是 ' 引号", "int")}) == ([], [])
