"""入口：一个进程里跑「轮询线程 + NiceGUI 面板」。

    ./run.sh start     后台启动
    ./run.sh fg        前台启动（调试用，日志直接打屏幕）

轮询在后台线程里跑，面板在主线程。两边都通过 db/store.py 的短连接读写数据库，
不共享任何可变状态，所以不需要加锁 —— 唯一共享的是各数据源的客户端实例
（sources.all_sources() 是进程内单例），每个实例自带一把锁保证本源串行发请求。
"""
import logging
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from nicegui import app, ui

import config
import sources
from core import poller
from db import store
from web import ui as webui


def setup_logging(console: bool | None = None) -> None:
    """console=None：有终端才往屏幕打（守护进程用）。True：强制打屏幕（前台命令用）。"""
    config.LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    # 【日志时间也按 JST 写】默认跟着机器时区走，而 NAS 的容器是 UTC —— 日志比库里、面板上的
    # 时间整整慢 9 小时，对着「10-27 03:00 推了一条」去翻日志，翻到的是 9 小时前的事。
    fmt.converter = lambda t: datetime.fromtimestamp(t, config.JST).timetuple()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 【只在真有终端时才往屏幕打】后台启动时 run.sh 把 stdout/stderr 也重定向进了
    # watch.log，而下面的 FileHandler 写的正是同一个文件 —— 两个 handler 各写一遍，
    # 日志里每条消息都会出现两次。前台跑（./run.sh fg）时 stderr 是 TTY，照常打屏幕。
    # `./run.sh once` 这类前台命令显式传 console=True：它们的输出不会被重定向进
    # watch.log，所以不会重复，而管道里（比如塞进 cron 或 tee）也得看得见结果。
    if console or (console is None and sys.stderr.isatty()):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    fh = RotatingFileHandler(config.LOG_DIR / "watch.log", maxBytes=5_000_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    # httpx 每发一个请求打一行 INFO。我们本来就是慢速轮询，这些行只会把真正有用的
    # 「降价」「售出」「描述否决」淹掉。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


STARTUP_RETRY_SEC = (5, 15, 30, 60)


def init_db(log, sleep=time.sleep) -> None:
    """建表 / 同步表结构 / 写默认设置。连接在启动时卡住或连不上，就换新连接重试几次。

    【为什么不直接退出交给 systemd】原先是一出错进程就退、systemd 10 秒后再拉起来。
    2026-10-16 NAS 到数据库的网络（路由器硬件转发的 bug）会把连发十几条查询的连接掐死，
    服务就这么转了 270 多圈，日志里全是同一段 traceback，看不出它在等什么。
    数据库那台机器重启的那几分钟也是一样。这里在进程内重试：每次都是新连接
    （出错的连接已经在 store.conn() 里丢掉了），日志写明第几次、等多久。
    【只重试连接类错误】SQL 写错、表结构冲突这类重试也没用的，照常直接抛出。
    """
    for i, wait in enumerate((*STARTUP_RETRY_SEC, None), 1):
        try:
            store.init_schema()
            return
        except store.CONN_ERRORS as e:
            if wait is None:
                raise
            log.warning("启动时连数据库失败（第 %d 次）：%s —— %d 秒后换新连接重试", i, e, wait)
            sleep(wait)


def main() -> None:
    setup_logging()
    log = logging.getLogger("main")

    init_db(log)
    if not store.get_rules():
        log.warning("一条规则都没有 —— 跑 `./run.sh seed` 写入两条起步规则，或到面板上新建。")

    log.info("数据源：%s", "、".join(f"{v.name}({k})" for k, v in sources.all_sources().items()))
    stop = threading.Event()
    thread = threading.Thread(target=poller.loop, args=(stop,), name="poller", daemon=True)
    # 看门狗：轮询线程卡死时唯一还能出声的东西。理由见 poller.watchdog。
    dog = threading.Thread(target=poller.watchdog, args=(stop,), name="watchdog", daemon=True)

    app.on_startup(thread.start)
    app.on_startup(dog.start)
    app.on_shutdown(stop.set)
    # 【关机时主动放租约】轮询线程可能正卡在一轮扫描中间、来不及走到自己的 release()，
    # 进程一退它就没了。这里在主线程再放一次，两处都放是双保险 —— 少一处就是
    # ./run.sh restart 之后白等 10 分钟。
    app.on_shutdown(poller.release)

    webui.create()
    log.info("面板 http://%s:%d", config.WEB_HOST, config.WEB_PORT)
    ui.run(host=config.WEB_HOST, port=config.WEB_PORT, title="Resale Watcher",
           favicon="🔍", reload=False, show=False)


# NiceGUI 在某些启动方式下会以 __mp_main__ 重新导入本模块，两个名字都要认
if __name__ in {"__main__", "__mp_main__"}:
    main()
