"""「失联」判定：源被限流时，旧数据不能冒充在售。

【为什么这一条必须有测试】它守的是一个静默失败：某个源被限流（实测 メルカリ 会回
403）之后，对账拉不到详情，判不出商品是卖掉了还是还挂着，于是它带着几小时前的
旧价格一直挂在命中页上，看起来和正常在售一模一样。实际发生过：一件 09-13 12:02
之后就再没见过的商品，在命中页上挂了 14.4 小时，而面板什么都没说。

界线写反的两个方向后果都不会报错：切多了静悄悄少几件（你以为这价位没货了），
切少了等于没做。
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from web.ui import split_stale, stale_hours  # noqa: E402


def item(hours_ago):
    return {"item_id": f"h{hours_ago}",
            "last_seen_at": config.now() - timedelta(hours=hours_ago)}


def test_失联小时数按最后一次见到算():
    assert abs(stale_hours(item(3)) - 3) < 0.01
    assert stale_hours(item(0)) < 0.01


def test_超过上限的撤下_没超的留着():
    rows = [item(0), item(1), item(23), item(25), item(200)]
    keep, dark = split_stale(rows, 24)
    assert [r["item_id"] for r in keep] == ["h0", "h1", "h23"]
    assert [r["item_id"] for r in dark] == ["h25", "h200"]


def test_一件都不丢_两边加起来等于原数():
    """撤下不是删除。少一件都算 bug —— 摘要里要报数，报错了人就发现不了。"""
    rows = [item(h) for h in (0, 5, 24, 24.5, 100)]
    keep, dark = split_stale(rows, 24)
    assert len(keep) + len(dark) == len(rows)
    assert {r["item_id"] for r in keep} | {r["item_id"] for r in dark} == \
           {r["item_id"] for r in rows}


def test_界线两侧分得清():
    """阈值的含义是「超过这么久才撤」。

    【不测"正好等于 24"】构造商品和量它之间隔着几微秒，stale_hours 会比 24 多一点点，
    测正好相等就是在跟时钟赛跑 —— 那种测试今天绿明天红，最后只会被人加 skip。
    这里测的是界线两侧分得清，那才是真正要锁的行为。
    """
    keep, dark = split_stale([item(23.99)], 24)
    assert len(keep) == 1 and not dark, "差一点点没到的不该被撤下"
    keep, dark = split_stale([item(24.01)], 24)
    assert not keep and len(dark) == 1, "刚过线的必须撤下"


def test_阈值设很大时一件都不撤():
    rows = [item(h) for h in (1, 50, 500)]
    keep, dark = split_stale(rows, 10_000)
    assert len(keep) == 3 and not dark
