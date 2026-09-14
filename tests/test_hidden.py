"""手动剔除：只把某一条从命中页拿掉，别的一概不动。

【为什么这条路径要测】它的两种写错方向都【不报错、不进日志】，而且页面上
看起来都很正常：
  剔多了 —— 同一个链接在两条规则下是两行 item，按 rule_id 去重的话会剔不干净，
            或者反过来把不该剔的一起带走，你只会觉得"这个价位怎么没货了"
  顺序错 —— 剔除排在 split_stale 后面的话，被剔的会先算进「另有 N 件已撤下」，
            那句话就开始说谎 —— 而它恰恰是用来让人相信没东西被偷偷藏起来的

【剔除不碰的东西也要锁住】它的全部承诺就是"只是不显示"。哪天有人顺手让它
也去动 matched / notified_at / tracked_at，这些用例会把那次改动拦下来。
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from web.ui import drop_hidden, split_stale  # noqa: E402


def item(src, iid, hours_ago=0.1):
    return {"source": src, "item_id": iid,
            "last_seen_at": config.now() - timedelta(hours=hours_ago)}


def test_只剔掉点名的那一条():
    rows = [item("mercari", "m1"), item("mercari", "m2"), item("yahoo_auction", "z1")]
    got = drop_hidden(rows, {("mercari", "m2")})
    assert [r["item_id"] for r in got] == ["m1", "z1"]


def test_同一个ID不同源不算同一件():
    """【源必须参与比对】三个源的 ID 体系各不相同，但撞号不是不可能。
    只比 item_id 的话，剔掉 メルカリ 的一件会顺手带走 ヤフオク 上一件不相干的，
    而你永远不会知道 —— 它只是没出现在列表里。
    """
    rows = [item("mercari", "x1"), item("yahoo_auction", "x1")]
    got = drop_hidden(rows, {("mercari", "x1")})
    assert [(r["source"], r["item_id"]) for r in got] == [("yahoo_auction", "x1")]


def test_同一个链接被两条规则抓到就一起剔掉():
    """【剔除按链接记，不按规则记】同一个商品被两条规则命中就是两行 item。
    你说「不想再看到它」指的是这件东西本身，不是它在某条规则下的那一行 ——
    要在每条规则下各剔一次的话，这个功能等于没做。
    """
    rows = [{**item("mercari", "m9"), "rule_id": 1},
            {**item("mercari", "m9"), "rule_id": 2}]
    assert drop_hidden(rows, {("mercari", "m9")}) == []


def test_没剔过任何东西时原样返回():
    rows = [item("mercari", "m1"), item("mercari", "m2")]
    assert drop_hidden(rows, set()) == rows


def test_剔除必须排在撤下前面():
    """【顺序反了那句摘要就开始说谎】页面上写的是「另有 N 件超过 24h 没见到，
    已撤下」。先 split_stale 再 drop_hidden 的话，一件既失联又被你剔掉的商品
    会被算进那个 N —— 而它根本不是被撤下的，是你自己剔的。
    这两个数分别对应两块完全不同的事（源在限流 / 你按了按钮），混在一起之后
    你会拿着一个假的「失联件数」去判断是不是该查限流。
    """
    rows = [item("mercari", "fresh"),
            item("mercari", "old", hours_ago=30),        # 失联
            item("mercari", "both", hours_ago=30)]       # 失联且被剔除
    hidden = {("mercari", "both")}

    kept = drop_hidden(rows, hidden)
    keep, dark = split_stale(kept, 24)
    assert [r["item_id"] for r in keep] == ["fresh"]
    assert [r["item_id"] for r in dark] == ["old"], "被剔除的混进了「已撤下」的计数里"


def test_剔除不许动商品本身():
    """【它的全部承诺就是"只是不显示"】set_hidden 只该写 hidden_item 这一张表。
    哪天有人顺手让它也去 UPDATE item（比如把 matched 置 0、或者标成已推），
    剔除就从"眼前清净一下"变成了"悄悄改判定"，而两者在页面上长得一模一样。
    """
    import inspect

    from db import store

    src = inspect.getsource(store.set_hidden)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    sql = code[code.index("if not on"):]
    assert "hidden_item" in sql
    assert "item " not in sql.replace("hidden_item", ""), "set_hidden 碰了 item 表"
    for col in ("matched", "notified_at", "tracked_at", "is_deal"):
        assert col not in sql, f"set_hidden 动了 item.{col} —— 剔除只该管显示"
