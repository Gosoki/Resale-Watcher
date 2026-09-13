"""标记（书签）。

【这一套守的是"标记要活得久"】标记唯一的用途是「以后回来查」，所以它的失败
全都是延迟发作的、而且发作时东西已经没了：
  挂在 item 表上      → 哪天删条规则，攒了几个月的标记跟着一起没
  只存 ID 不存快照    → 商品下架后打开这一页只剩一排死链接
  重复标记覆盖快照    → 手滑点两下，"我当初看到的是多少钱"变成了现在的价
这三条都不会报错、不会进日志，只会在你需要它的那天发现是空的。
"""
import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402

ROW = {"source": "mercari", "item_id": "m1", "name": "RTX 5090 GAMING TRIO",
       "price": 870_000, "thumb_url": "https://x/y.jpg"}


class Spy:
    """顶掉 store.execute，把发出去的 SQL 和参数记下来。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def __call__(self, sql, args=()):
        self.calls.append((" ".join(sql.split()), tuple(args)))
        return 1


def run(fn, *a, **kw):
    spy, saved = Spy(), store.execute
    store.execute = spy
    try:
        fn(*a, **kw)
    finally:
        store.execute = saved
    return spy.calls


# ---------------------------------------------------------------- 快照

def test_标记存的是快照不是引用():
    (sql, args), = run(store.set_marked, ROW, "RTX 5090 单卡", True)
    assert "INSERT INTO marked_item" in sql
    for v in (ROW["name"], ROW["price"], ROW["thumb_url"], "RTX 5090 单卡"):
        assert v in args, f"{v!r} 没进快照 —— 商品下架后这一页就少一块"


def test_重复标记不覆盖快照也不改时间():
    """手滑点两下不该把"我当初看到的"改成"现在的"。
    ON DUPLICATE 必须是个空操作（item_id = item_id）。"""
    (sql, _), = run(store.set_marked, ROW, "r", True)
    m = re.search(r"ON DUPLICATE KEY UPDATE (.+?)$", sql)
    assert m, "没有 ON DUPLICATE 子句：第二次标记会直接抛主键冲突"
    assert m.group(1).strip() == "item_id = item_id", (
        f"ON DUPLICATE 改了东西（{m.group(1)!r}）—— 快照或时间会被第二次点击冲掉")


def test_取消标记只删自己那一行():
    (sql, args), = run(store.set_marked, ROW, "r", False)
    assert sql.startswith("DELETE FROM marked_item")
    assert args == ("mercari", "m1")


# ---------------------------------------------------------------- 活得久

def test_删规则不能连坐删掉标记():
    """delete_rule 会 DELETE FROM item WHERE rule_id=…。标记要是挂在 item 上，
    重建一次规则就全没了 —— 而这正是这张表单独存在的唯一理由。"""
    src = inspect.getsource(store.delete_rule)
    assert "marked_item" not in src, (
        "delete_rule 动了 marked_item：删条规则就会把标记一起清掉")


def test_标记表独立且不带_rule_id():
    """带 rule_id 的话，规则删了就只剩一个查不到的数字；
    而且同一个商品被两条规则抓到会变成两条标记。"""
    sql = (Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text()
    m = re.search(r"CREATE TABLE IF NOT EXISTS marked_item \((.*?)\n\) ENGINE", sql, re.S)
    assert m, "schema.sql 里没有 marked_item 表"
    body = m.group(1)
    assert "PRIMARY KEY (source, item_id)" in body
    # 【按列名匹配，不能用 in】注释里就写着"存名字不存 rule_id"这句话，
    # 直接判子串会被自己的注释绊倒
    assert not re.search(r"^\s+rule_id\s", body, re.M), \
        "标记表不该带 rule_id 列，只存 rule_name 做展示"
    for col in ("name", "price", "thumb_url", "marked_at"):
        assert re.search(rf"^\s+{col}\s", body, re.M), f"快照少了 {col} 列"
