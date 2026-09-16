"""轮询线程卡死之后，谁来出声。

【真事】2026-09-15 10:47 网络抖了一下，MySQL 服务器把连接丢了，客户端那头的
socket 还是 ESTABLISHED、卡在 recv() 里 —— pymysql 默认 read_timeout=None。
轮询线程就这么停了 40 小时：进程活着、面板开着、顶栏红字写着「轮询已停止」，
但没人盯着面板。（NAS 上另一份实例碰巧接了盘，纯属运气。）

这里锁三件事：
  1. 连接必须带超时 —— 卡死的根因
  2. 心跳停太久要告警，且一个停机段只告警一次 —— 不然停一次推 60 条，人会关掉推送
  3. 轮询不许起第二个 —— 起两个不报错，只是同一个源被打两遍
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import poller  # noqa: E402

T0 = datetime(2026, 9, 15, 10, 47, 59)


# ---------------------------------------------------------------- 1. 超时

def test_数据库连接必须带三个超时():
    """pymysql 默认 read_timeout=None 是无限等。少一个，下一次网络抖动就是下一次 40 小时。"""
    for k in ("connect_timeout", "read_timeout", "write_timeout"):
        assert config.DB.get(k), f"config.DB 少了 {k} —— 又回到无限等了"
        assert 0 < config.DB[k] <= 120, f"{k}={config.DB[k]} 不像个超时值"


def test_连接参数真的传给了pymysql():
    """config.DB 里有不算数，得真的进 pymysql.connect 的 kwargs。"""
    from db import store

    got = {}

    class Fake:
        def close(self):
            pass

    saved = store.pymysql.connect
    store._drop_conn()                    # 本线程可能缓存着前面用例建的真连接
    store.pymysql.connect = lambda **kw: got.update(kw) or Fake()
    try:
        with store.conn():
            pass
    finally:
        store.pymysql.connect = saved
        store._drop_conn()                # 别把假连接留给后面的用例
    assert got["read_timeout"] == config.DB["read_timeout"]
    assert got["connect_timeout"] == config.DB["connect_timeout"]


# ---------------------------------------------------------------- 2. 判停

def test_没停或没到阈值不算停():
    assert poller.stalled_for(T0 + timedelta(minutes=9), T0, 10) is None
    assert poller.stalled_for(T0 + timedelta(minutes=10), T0, 10) == 10
    assert poller.stalled_for(T0 + timedelta(hours=40), T0, 10) == 2400


def test_阈值为0等于关闭():
    assert poller.stalled_for(T0 + timedelta(hours=40), T0, 0) is None


def test_还没起来过不算停():
    """进程刚起、轮询线程还没转第一圈时 heartbeat 是 None —— 那不是卡死。"""
    assert poller.stalled_for(T0, None, 10) is None


# ---------------------------------------------------------------- 3. 看门狗

class Clock:
    """可拨的时钟 + 可控的 stop_event.wait。"""

    def __init__(self, start):
        self.now = start
        self.rounds = 0
        self.max_rounds = 0

    def wait(self, _):
        # 每 wait 一次算一轮，时间前进一分钟；轮数用完就"停止"
        self.rounds += 1
        self.now += timedelta(minutes=1)
        return self.rounds > self.max_rounds

    def is_set(self):
        return False


def run_dog(rounds, beat, settings, start=T0):
    """跑 rounds 轮看门狗，返回发出去的告警列表。心跳固定不动（模拟卡死）。"""
    from db import store

    clock = Clock(start)
    clock.max_rounds = rounds
    sent = []
    saved_now, saved_beat, saved_settings = config.now, poller._beat["at"], store.get_settings
    config.now = lambda: clock.now
    poller._beat["at"] = beat
    store.get_settings = lambda force=False: settings
    try:
        poller.watchdog(clock, alert=lambda url, tpl, text: sent.append(text))
    finally:
        config.now, poller._beat["at"], store.get_settings = saved_now, saved_beat, saved_settings
    return sent


ON = {"poller_stall_min": 10, "notify_url": "u", "notify_body": ""}


def test_停够阈值才告警_且一个停机段只告警一次():
    """【一个停机段只发一条】不然停一次每分钟推一条，你第一件事就是把推送关掉。"""
    sent = run_dog(rounds=60, beat=T0, settings=ON)
    assert len(sent) == 1, f"60 分钟里发了 {len(sent)} 条，应该只有 1 条"
    assert "轮询已停" in sent[0] and "10 分钟" in sent[0]
    assert "09-15 10:47" in sent[0], "告警里得写明心跳停在什么时候"


def test_持续不恢复每6小时再提醒一次():
    sent = run_dog(rounds=13 * 60, beat=T0, settings=ON)          # 13 小时
    assert len(sent) == 3, f"13 小时应该是 3 条（第 10 分钟、6 小时后、12 小时后），实际 {len(sent)}"


def test_没填推送地址只写日志不发():
    sent = run_dog(rounds=30, beat=T0, settings={**ON, "notify_url": ""})
    assert sent == []


def test_关闭时一条都不发():
    sent = run_dog(rounds=30, beat=T0, settings={**ON, "poller_stall_min": 0})
    assert sent == []


def test_看门狗自己出错不会死():
    """它防的就是别的线程卡死 —— 自己先崩了等于没有。"""
    from db import store

    clock = Clock(T0)
    clock.max_rounds = 5
    saved_now, saved_settings = config.now, store.get_settings
    config.now = lambda: clock.now

    def boom(force=False):
        raise RuntimeError("库抽风")

    store.get_settings = boom
    try:
        poller.watchdog(clock, alert=lambda *a: None)      # 不抛就算过
    finally:
        config.now, store.get_settings = saved_now, saved_settings
    assert clock.rounds == 6, "出错之后应该继续转，而不是退出"


# ---------------------------------------------------------------- 4. 防双启

def test_轮询不许起第二个():
    """起两个的后果不是报错，是同一个源被打两遍，而两边日志长得一模一样。"""
    saved = poller._beat["running"]
    poller._beat["running"] = True
    try:
        class Stop:
            def is_set(self):
                raise AssertionError("第二个实例不该进主循环")
        poller.loop(Stop())                # 应该直接 return，不进 while
    finally:
        poller._beat["running"] = saved
