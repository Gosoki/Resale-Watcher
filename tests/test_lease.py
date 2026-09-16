"""轮询租约：多份实例共用一个库时，只许一份真的去抓。

【为什么要这个】2026-09-17 从 MySQL 的 performance_schema.hosts 反查，发现 NAS 上
早就跑着一份同样的东西、写同一个库。两份各自限速，同一个公网 IP 下对平台的
请求密度翻倍 —— メルカリ 一天一百多次 403 多半就是这么来的。
而 09-15 本机轮询线程卡死 40 小时时，是 NAS 那份碰巧接了盘。租约把"碰巧"变成"设计"：
持有者停了，到期后待命的那份自动接手。

【这里锁什么】租约写错的两个方向都不报错：
  两边都以为自己拿到了 → 回到双抓，和没做一样
  谁都拿不到           → 谁都不抓，面板一切正常、心跳照跳，只是库里再也没有新东西
"""
import pathlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import poller  # noqa: E402
from db import store  # noqa: E402

T0 = datetime(2026, 9, 17, 3, 0, 0)


class FakeLeaseDB:
    """只模拟 poller_lease 那一行，按 store 里三条 SQL 的语义实现。"""

    def __init__(self):
        self.row = None

    def claim(self, holder, ttl, now):
        until = now + timedelta(seconds=ttl)
        if self.row is None:                                # INSERT IGNORE
            self.row = {"holder": holder, "taken_at": now, "expires_at": until}
            return holder
        r = self.row
        if r["holder"] == holder or r["expires_at"] < now:  # UPDATE ... WHERE
            if r["holder"] != holder:
                r["taken_at"] = now
            r["holder"], r["expires_at"] = holder, until
        return r["holder"]

    def renew(self, holder, ttl, now):
        if self.row and self.row["holder"] == holder:
            self.row["expires_at"] = now + timedelta(seconds=ttl)


def wire(db, now_box, settings):
    """把 poller 接到假库和假时钟上，返回还原函数。"""
    saved = (store.claim_lease, store.renew_lease, store.get_settings, config.now,
             dict(poller._beat))
    store.claim_lease = lambda h, ttl: db.claim(h, ttl, now_box[0])
    store.renew_lease = lambda h, ttl: db.renew(h, ttl, now_box[0])
    store.get_settings = lambda force=False: settings
    config.now = lambda: now_box[0]
    poller._beat["holder"] = None

    def restore():
        store.claim_lease, store.renew_lease, store.get_settings, config.now, beat = saved
        poller._beat.update(beat)
    return restore


def test_空表时第一个来的拿到():
    db, now = FakeLeaseDB(), [T0]
    restore = wire(db, now, {"poller_lease_sec": 300})
    try:
        assert poller._claim() is True
        assert poller.lease_holder() == poller.ME
    finally:
        restore()


def test_别人持有且没过期时拿不到():
    db, now = FakeLeaseDB(), [T0]
    db.claim("nas:1", 300, T0)
    restore = wire(db, now, {"poller_lease_sec": 300})
    try:
        assert poller._claim() is False, "别人的租约还有效，不该抢"
        assert poller.lease_holder() == "nas:1"
    finally:
        restore()


def test_别人过期了就接手():
    """【这就是 09-15 那天该自动发生的事】持有者卡死，到期后待命的接手。"""
    db, now = FakeLeaseDB(), [T0]
    db.claim("nas:1", 300, T0)
    now[0] = T0 + timedelta(seconds=301)
    restore = wire(db, now, {"poller_lease_sec": 300})
    try:
        assert poller._claim() is True
        assert db.row["holder"] == poller.ME
        assert db.row["taken_at"] == now[0], "换人时 taken_at 要更新"
    finally:
        restore()


def test_持有者续期后别人接不了():
    """【续期是防误换手的关键】一轮要跑十几分钟，只在循环顶部争一次的话，
    跑到一半租约就过期了，另一份接手 —— 两边同时在抓，和没做一样。
    """
    db, now = FakeLeaseDB(), [T0]
    restore = wire(db, now, {"poller_lease_sec": 300})
    try:
        assert poller._claim() is True
        now[0] = T0 + timedelta(seconds=250)
        poller._renew()                                     # 扫完一个源
        now[0] = T0 + timedelta(seconds=400)                # 距首次拿到已 400s，距续期 150s
        assert db.claim("nas:1", 300, now[0]) == poller.ME, "续过期了，别人不该接手"
    finally:
        restore()


def test_不是持有者时续期不会抢():
    db, now = FakeLeaseDB(), [T0]
    db.claim("nas:1", 300, T0)
    restore = wire(db, now, {"poller_lease_sec": 300})
    try:
        poller._claim()                                     # 没拿到
        poller._renew()
        assert db.row["holder"] == "nas:1"
    finally:
        restore()


def test_租约关闭时永远能抓():
    """poller_lease_sec=0 ＝ 各抓各的（老行为）。而且一条租约 SQL 都不该发。"""
    db, now = FakeLeaseDB(), [T0]
    db.claim("nas:1", 300, T0)
    restore = wire(db, now, {"poller_lease_sec": 0})
    try:
        assert poller._claim() is True
        assert poller.lease_holder() is None
        assert db.row["holder"] == "nas:1", "关闭时不该碰租约表"
    finally:
        restore()


def test_没拿到租约时主循环一个源都不扫():
    """【锁的是 loop 里的那个 continue】漏了它，租约就只是个日志里的摆设。"""
    import inspect

    src = inspect.getsource(poller.loop)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    i = code.index("_claim()")
    j = code.index("refresh_tracked()")
    assert i < j, "争租约必须在追踪刷新和整轮扫描之前"
    assert "continue" in code[i:j], "没拿到租约时必须 continue，跳过整轮"


def test_争租约的SQL是原子的():
    """【判断和写入必须在同一条 UPDATE 里】先 SELECT 再 UPDATE 的话，
    两个实例会同时看到"过期了"、同时写、都以为自己拿到了。
    """
    import inspect

    src = inspect.getsource(store.claim_lease)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    upd = code[code.index("UPDATE poller_lease"):]
    assert "WHERE id = 1 AND (holder = %s OR expires_at < %s)" in upd
    # taken_at 必须在 holder 之前赋值（MySQL SET 从左到右生效）
    assert upd.index("taken_at = IF(") < upd.index("holder = %s, expires_at")


def test_退出时放掉自己的租约_下一个进程立刻能接():
    """【不放的话 ./run.sh restart 白等 10 分钟】新进程看到旧进程的租约没到期，老实待命。"""
    db, now = FakeLeaseDB(), [T0]
    restore = wire(db, now, {"poller_lease_sec": 600})
    saved_release = store.release_lease

    def fake_release(holder):                 # 和 store.release_lease 一样：删行
        if db.row and db.row["holder"] == holder:
            db.row = None
    store.release_lease = fake_release
    try:
        assert poller._claim() is True
        poller.release()
        assert poller.lease_holder() is None
        # 下一个进程（换个名字）在同一秒就能拿到
        assert db.claim("next:9", 600, now[0]) == "next:9"
    finally:
        store.release_lease = saved_release
        restore()


def test_不是持有者时退出不碰租约():
    db, now = FakeLeaseDB(), [T0]
    db.claim("nas:1", 600, T0)
    restore = wire(db, now, {"poller_lease_sec": 600})
    calls = []
    saved_release = store.release_lease
    store.release_lease = lambda h: calls.append(h)
    try:
        poller._claim()                       # 没拿到
        poller.release()
        assert calls == [], "不是自己的租约不该去动"
    finally:
        store.release_lease = saved_release
        restore()


def test_主线程退出钩子也会放租约():
    """轮询线程可能正卡在扫描中间来不及放，主线程的 on_shutdown 是第二道保险。"""
    src = pathlib.Path(__file__).resolve().parent.parent.joinpath("main.py").read_text()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "app.on_shutdown(poller.release)" in code
