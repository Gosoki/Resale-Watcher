"""启动时连不上数据库：换新连接重试几次，而不是一出错就退出、靠 systemd 反复拉起。

2026-10-16 NAS 到数据库的网络（路由器硬件转发的 bug）把启动时连发的查询掐死，
服务一出错就退、systemd 10 秒后再起，转了 270 多圈，日志里看不出它在等什么。
"""
import logging
import sys
from pathlib import Path

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from db import store  # noqa: E402

LOG = logging.getLogger("test")


def _flaky(monkeypatch, errors):
    """前几次按 errors 依次抛错，之后成功。"""
    calls = []

    def init():
        calls.append(1)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
    monkeypatch.setattr(store, "init_schema", init)
    return calls


def test_连接卡住或连不上_换新连接重试到成功(monkeypatch):
    errs = [pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query"),
            pymysql.err.OperationalError(2003, "Can't connect to MySQL server")]
    calls, slept = _flaky(monkeypatch, errs), []
    main.init_db(LOG, sleep=slept.append)
    assert len(calls) == 3 and slept == list(main.STARTUP_RETRY_SEC[:2])


def test_重试用完了还不行_照常抛出让进程退出(monkeypatch):
    n = len(main.STARTUP_RETRY_SEC) + 1
    errs = [pymysql.err.OperationalError(2003, "down")] * n
    calls, slept = _flaky(monkeypatch, errs), []
    with pytest.raises(pymysql.err.OperationalError):
        main.init_db(LOG, sleep=slept.append)
    assert len(calls) == n and slept == list(main.STARTUP_RETRY_SEC)


def test_不是连接类的错误_不重试直接抛出(monkeypatch):
    # SQL 写错、表结构冲突：重试也没用，掩盖它只会让问题更难查
    calls, slept = _flaky(monkeypatch, [pymysql.err.ProgrammingError(1064, "syntax error")]), []
    with pytest.raises(pymysql.err.ProgrammingError):
        main.init_db(LOG, sleep=slept.append)
    assert len(calls) == 1 and slept == []
