"""每个线程一条长连接。

【为什么要这个】原先每条 SQL 开一条短连接。实测到 10.0.10.20：建连 31ms、
同一条连接上 SELECT 1 只要 3ms —— 每条查询 90% 的时间在握手。命中页 19 条查询
803ms，复用后 98ms。面板上每一处"卡一下"根子都在这里。

【这里锁什么】复用写错的方向都不会在测试里报错，只会在线上：
  跨线程共用      → 某天撞出 "Packet sequence number wrong"
  闲置后不检查    → 工作线程闲一夜，第一条查询 "MySQL server has gone away"
  出错后不丢弃    → 坏连接一直留着，每条查询都失败
  db=False 也缓存 → 建库那条没选库，后面全部 No database selected
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402


class FakeConn:
    n = 0

    def __init__(self, **kw):
        FakeConn.n += 1
        self.id = FakeConn.n
        self.kw = kw
        self.alive = True
        self.closed = False
        self.pinged = 0

    def ping(self, reconnect=False):
        self.pinged += 1
        if not self.alive:
            raise store.pymysql.err.OperationalError(2006, "MySQL server has gone away")

    def close(self):
        self.closed = True

    def cursor(self):
        conn = self

        class Cur:
            rowcount = 1

            def __enter__(s):
                return s

            def __exit__(s, *a):
                pass

            def execute(s, sql, args=None):
                if not conn.alive:
                    raise store.pymysql.err.OperationalError(2013, "Lost connection")
                s.sql = sql

            def fetchall(s):
                return [{"sql": s.sql, "conn": conn.id}]
        return Cur()


def with_fake(fn):
    """把 pymysql.connect 换成假的，跑完还原并清掉本线程的缓存连接。"""
    saved = store.pymysql.connect
    store._drop_conn()
    FakeConn.n = 0
    store.pymysql.connect = lambda **kw: FakeConn(**kw)
    try:
        return fn()
    finally:
        store.pymysql.connect = saved
        store._drop_conn()


def test_同一线程连续查询复用同一条连接():
    def go():
        a = store.query("SELECT 1")[0]["conn"]
        b = store.query("SELECT 2")[0]["conn"]
        store.execute("UPDATE x")
        return a, b, FakeConn.n
    a, b, n = with_fake(go)
    assert a == b == 1 and n == 1, f"三条 SQL 应该只建一条连接，实际建了 {n}"


def test_不同线程各持一条():
    """PyMySQL 的连接不是线程安全的：轮询线程和事件循环共用一条会撞包序号。"""
    def go():
        ids = []
        store.query("SELECT 1")
        t = threading.Thread(target=lambda: ids.append(store.query("SELECT 1")[0]["conn"]))
        t.start(); t.join()
        ids.append(store.query("SELECT 1")[0]["conn"])
        return ids, FakeConn.n
    ids, n = with_fake(go)
    assert ids[0] != ids[1], "两个线程拿到了同一条连接"
    assert n == 2


def test_闲置太久先ping_死了就重建():
    """io_bound 的工作线程闲一夜，wait_timeout=8h 早把它踢了。"""
    def go():
        c1 = store.query("SELECT 1")[0]["conn"]
        conn = store._local.c
        conn.alive = False                        # 服务器那头把它踢了
        store._local.used -= store.IDLE_PING_SEC + 1   # 假装闲置很久
        c2 = store.query("SELECT 1")[0]["conn"]
        return c1, c2, conn.pinged, conn.closed
    c1, c2, pinged, closed = with_fake(go)
    assert pinged == 1, "闲置之后用之前该 ping 一次"
    assert c2 != c1, "ping 失败应该换一条新连接"
    assert closed, "坏掉的那条要 close"


def test_没闲置就不ping():
    """每条都 ping 会把收益砍掉一半（成交页实测 60ms → 108ms）。"""
    def go():
        store.query("SELECT 1")
        store.query("SELECT 2")
        return store._local.c.pinged
    assert with_fake(go) == 0


def test_执行中断线就丢掉这条_下次重建_但不自动重试():
    """【不重试】INSERT price_log / watch_rule 不是幂等的，盲目重试会多出一行。"""
    def go():
        store.query("SELECT 1")
        conn = store._local.c
        conn.alive = False
        try:
            store.execute("INSERT INTO price_log ...")
        except store.pymysql.err.OperationalError:
            pass
        else:
            raise AssertionError("断线的执行应该抛出去让调用方知道")
        assert conn.closed and store._local.c is None, "坏连接没被丢掉"
        c2 = store.query("SELECT 1")[0]["conn"]
        return conn.id, c2
    old, new = with_fake(go)
    assert new != old


def test_建库用的短连接不进缓存():
    """db=False 没选库，缓存下来后面全部 No database selected。"""
    def go():
        with store.conn(db=False) as c:
            assert "database" not in c.kw
            assert not c.closed
        assert c.closed, "短连接用完要关"
        assert getattr(store._local, "c", None) is None, "短连接不该被缓存"
    with_fake(go)


def test_线程局部连接带着三个超时():
    def go():
        store.query("SELECT 1")
        return store._local.c.kw
    kw = with_fake(go)
    for k in ("connect_timeout", "read_timeout", "write_timeout"):
        assert kw.get(k), f"长连接少了 {k} —— 又回到无限等了"
