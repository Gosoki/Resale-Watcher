"""顶栏诊断 + 面板异步动作的三个口子（2026-09-17 体检）。

  顶栏  轮询停了只显示「心跳停在 10:47:59」—— 没日期、没时长、不说该做什么；
        配额用完时主循环一睡 10 分钟，顶栏却报「轮询已停止」，人照着去重启白折腾；
        总失败数看不出是哪个源在失败。
  动作  「立即跑一轮」没有闸，连点就是 N 个 run_once 并行；跑完之后 notify 落在
        已被 refresh 删掉的按钮上抛 RuntimeError，提示和后面的刷新一起丢掉。
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import ui as webui  # noqa: E402


def code_of(fn) -> str:
    src = inspect.getsource(fn)
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


# ---------------------------------------------------------------- 顶栏

def test_时长格式():
    assert webui.fmt_minutes(9) == "9 分钟"
    assert webui.fmt_minutes(60) == "1 小时"
    assert webui.fmt_minutes(75) == "1 小时 15 分"
    assert webui.fmt_minutes(2400) == "1 天 16 小时"
    assert webui.fmt_minutes(48 * 60) == "2 天"


def test_顶栏先判配额再判停摆():
    """配额用完后主循环一睡 10 分钟，心跳看起来也像停了。判反了的话红字写着
    「轮询已停止」，人照着去重启 —— 计数在库里按日期走，重启毫无用处。"""
    src = code_of(webui.create)
    i = src.index("if quota_out:")
    j = src.index("stalled is not None")
    assert i < j, "配额分支必须排在停摆分支前面"
    assert "0 点（JST）自动恢复" in src and "不用重启" in src


def test_顶栏停摆要说时长和日期():
    src = code_of(webui.create)
    assert "fmt_minutes(stalled)" in src
    i = src.index("心跳停在")
    assert "%m-%d %H:%M" in src[i:i + 80], "只给时分秒的话，停了两天你看不出来"


def test_顶栏不再单独查今日合计():
    """today_by_source 已经把每个源的 requests/errors 取回来了，合计加一下就是。"""
    src = code_of(webui.create)
    tick = src[src.index("def tick()"):src.index("ui.timer(10.0, tick)")]
    assert "today_stat" not in tick
    assert "today_by_source" in tick


# ---------------------------------------------------------------- 动作

def test_立即跑一轮也有闸():
    src = code_of(webui.run_now)
    assert '_fetching["busy"]' in src and "finally" in src, "没有闸就是连点几下几个 run_once 并行"


def test_三个手动动作都走扫描闸():
    """后台轮询和面板手动动作抢同一把锁，拿不到抛 ScanBusy → 弹提示，不是闷头等。"""
    for fn in (webui.fetch_all, webui.pull_tracked, webui.run_now):
        src = code_of(fn)
        assert "poller.manual" in src, f"{fn.__name__} 没走 poller.manual"
        assert "ScanBusy" in src, f"{fn.__name__} 没接 ScanBusy"


def test_异步动作在await之前拿client_之后用它弹提示():
    """await 回来时发起动作的按钮可能已经被别的 refresh 删了；用 client 当上下文
    就不依赖那个按钮。await 之后再拿就晚了 —— 那一刻 slot 已经死了。"""
    for fn in (webui.fetch_all, webui.pull_tracked, webui.run_now, webui.test_notify):
        src = code_of(fn)
        i = src.index("client = ui.context.client")
        j = src.index("await ")
        assert i < j, f"{fn.__name__}：client 必须在第一个 await 之前拿"
        assert "with client:" in src[j:], f"{fn.__name__}：await 之后的提示要在 with client: 里"


def test_notify自己也兜住已删除的容器():
    src = code_of(webui.notify)
    assert "except RuntimeError" in src
