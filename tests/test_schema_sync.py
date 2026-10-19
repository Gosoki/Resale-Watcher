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


def test_写默认设置只发一条INSERT_外加清孤儿的一条DELETE(monkeypatch):
    """启动路径上的来回越少越好：2026-10-16 NAS 到数据库的网络会在一条连接连发十几条之后
    把连接掐死，服务卡在逐项 INSERT 的第 10 条上、反复重启 270 多次。根因在网络，
    但这一步本来就没必要一项一条。语义不能变：v 只在首次插入时写，note 每次都覆盖。"""
    import config
    from db import store
    calls = []
    monkeypatch.setattr(store, "execute", lambda sql, args=(): calls.append((sql, list(args))))
    store.seed_settings()
    inserts = [c for c in calls if c[0].startswith("INSERT")]
    assert len(inserts) == 1 and len(calls) == 2, [c[0][:40] for c in calls]
    sql, args = inserts[0]
    assert sql.count("(%s, %s, %s, %s)") == len(config.SETTINGS_SPEC)
    assert sql.rstrip().endswith("ON DUPLICATE KEY UPDATE note = VALUES(note)"), "v 不能被覆盖"
    assert args[0::4] == list(config.SETTINGS_SPEC), "每行的 k 按 SPEC 顺序"
    assert calls[1][0].startswith("DELETE FROM app_setting WHERE k NOT IN")


def test_改了关键词_换了源_重新启用时_清掉旧的平台总数(monkeypatch):
    """轮询拿上一轮的 last_total 判「解析突然失效」（上一轮一堆、这一轮 0 件）。把关键词改窄、
    新词在某个源上本来就 0 件时，旧词的数会让它被一直误判成改版、永远不做售出对账。"""
    from db import store
    for changed, expect_reset in ((["keyword"], True), (["sources"], True), (["enabled"], True),
                                  (["price_max", "note"], False)):
        calls = []
        monkeypatch.setattr(store, "execute", lambda sql, args=(): calls.append(sql))
        store.update_rule(7, {k: 1 for k in changed})
        reset = [c for c in calls if c.startswith("UPDATE rule_source_state SET last_total = 0")]
        assert bool(reset) is expect_reset, (changed, calls)


class _FakeCur:
    def __init__(self, tables, log):
        self.tables, self.log = tables, log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.log.append(sql.split("(")[0].strip()[:40])

    def fetchall(self):
        return [{"TABLE_NAME": t} for t in self.tables]


def _run_init(monkeypatch, tables):
    import contextlib
    from db import store
    log = []

    @contextlib.contextmanager
    def fake_conn(db=True):
        yield type("C", (), {"cursor": lambda self: _FakeCur(tables, log)})()
    monkeypatch.setattr(store, "conn", fake_conn)
    monkeypatch.setattr(store, "sync_schema", lambda: log.append("sync_schema"))
    monkeypatch.setattr(store, "seed_settings", lambda: log.append("seed_settings"))
    store.init_schema()
    return log


def test_表都在时启动只查一次表_不重跑十几条建表语句(monkeypatch):
    """启动路径上的来回越少越好：2026-10-16 NAS 到数据库的网络会把连发十几条的连接掐死。"""
    import re
    from db import store
    sql = store.SCHEMA_PATH.read_text(encoding="utf-8")
    tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql)
    log = _run_init(monkeypatch, tables)
    assert log[0].startswith("SELECT TABLE_NAME"), log
    assert not any(x.startswith("CREATE") for x in log), log
    assert log[-2:] == ["sync_schema", "seed_settings"]


def test_缺一张表时照常把建表语句全跑一遍(monkeypatch):
    import re
    from db import store
    sql = store.SCHEMA_PATH.read_text(encoding="utf-8")
    tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql)
    log = _run_init(monkeypatch, tables[:-1])
    creates = [x for x in log if x.startswith("CREATE TABLE")]
    assert len(creates) == len(tables) and any(x.startswith("CREATE DATABASE") for x in log)
