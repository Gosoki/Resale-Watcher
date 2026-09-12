"""卖家黑名单。

这个功能有三处会静默失效，每一处都是"看起来在跑、其实没生效"：

  1. revalidate 的 SELECT 漏掉 seller_id —— 扫描时判成不合适，紧接着重判又放回来，
     两个动作在同一轮里互相抵消，日志和面板都没有任何异常
  2. メルカリShops 的 sellerId 是哨兵 "0" 而不是真卖家 —— 拉黑"0"会一次误杀
     几十件分属不同店铺的商品，而人以为自己只拉黑了一家
  3. 卖家ID为空时若按"不在白名单里"处理，会把整批不给卖家ID的商品清零

下面的用例分别锁这三条。
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import poller  # noqa: E402
from core.matcher import explain, judge_snap  # noqa: E402
from core.normalize import ids  # noqa: E402
from sources.mercari import _seller  # noqa: E402

RULE = {
    "include_all": "5090", "include_any": "", "exclude_any": "ジャンク", "warn_desc": "",
    "exclude_sellers": "917987475, 2yhr98NDVi1eGuLhNbYtU5Z6",
    "price_min": 0, "price_max": 0, "condition_ids": "", "allow_shops": 1,
}


def _strip_comments(src: str) -> str:
    return "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))


def _judge_callers():
    """所有把【库里的行】喂给 judge_snap 的地方。每一处都有自己的一份 SELECT。"""
    import tools.replay
    return [(poller.revalidate, "poller.revalidate"), (tools.replay.replay, "tools.replay")]


def snap(**kw):
    base = {"name": "RTX 5090 グラフィックボード", "price": 700000, "seller_id": "",
            "item_type": "user", "condition_id": 2}
    return {**base, **kw}


# ---------------------------------------------------------------- 基本判定

def test_黑名单里的卖家判不合适但仍入库():
    """入库而不是丢弃：取消拉黑后 revalidate 能把它们重判回命中，不用等重扫。"""
    v = judge_snap(RULE, snap(seller_id="917987475"))
    assert v == {"keep": True, "matched": 0, "reject_reason": "seller"}


def test_不在黑名单里的卖家正常命中():
    assert judge_snap(RULE, snap(seller_id="123456"))["matched"] == 1


def test_大小写不敏感():
    """ヤフオク 的 ID 是混合大小写的，手抄时大小写出入不该导致拉黑失效。"""
    assert judge_snap(RULE, snap(seller_id="2YHR98ndvI1EgUlHnBytu5z6"))["reject_reason"] == "seller"


def test_黑名单优先于标题排除词():
    """拉黑之后在「全部」页却看到「标题排除词」，人会以为拉黑没生效。"""
    v = judge_snap(RULE, snap(seller_id="917987475", name="RTX 5090 ジャンク"))
    assert v["reject_reason"] == "seller"


# ---------------------------------------------------------------- 卖家未知

def test_卖家ID为空时一律放行():
    """ヤフオク 有一部分商品不给卖家ID。按"不在白名单里"处理会把它们整批清零，
    而你在命中页什么都看不到，只会以为那边没货。不知道 ≠ 命中。"""
    assert judge_snap(RULE, snap(seller_id=""))["matched"] == 1
    assert judge_snap(RULE, snap(seller_id=None))["matched"] == 1


def test_メルカリShops_的哨兵_0_不是卖家():
    """Shops 商品的卖家是店铺实体不是用户，接口一律返回 sellerId="0"。
    实测库里 41 件顶着这个"卖家ID"，分属不同店铺 —— 原样留着的话，
    黑名单里填一个 0 就会一次误杀这 41 件。"""
    assert _seller("0") == ""
    assert _seller(None) == ""
    assert _seller("") == ""
    assert _seller("917987475") == "917987475"


def test_哨兵归零后黑名单填0也伤不到人():
    rule = dict(RULE, exclude_sellers="0")
    assert judge_snap(rule, snap(seller_id=_seller("0")))["matched"] == 1


# ---------------------------------------------------------------- ID 拆分

def test_ids_不剥符号_和_norm_不是一回事():
    """norm() 会把非字母数字全删掉，那是给商品标题用的。
    卖家ID 是标识符，剥了可能把两个不同的 ID 抹成同一个。"""
    assert ids("a-b, a_b") == {"a-b", "a_b"}       # norm() 会把这两个都变成 "ab"
    assert ids("  AbC , , p123 ") == {"abc", "p123"}
    assert ids("") == set() and ids(None) == set()


def test_ids_认全角逗号和换行():
    assert ids("a，b、c\nd") == {"a", "b", "c", "d"}


# ---------------------------------------------------------------- 说明列

def test_全部页能说出是谁被拉黑了():
    row = {"reject_reason": "seller", "seller_id": "917987475", "price": 1, "name": ""}
    assert "917987475" in explain(RULE, row)


# ---------------------------------------------------------------- 静默失效防线

def test_revalidate_必须查出_judge_snap_读的每一个字段():
    """【这条是本文件存在的主要理由】revalidate 每轮都拿库里的行重判一遍。
    它的 SELECT 少一个字段，judge_snap 就会在重判时读到 None ——
    于是扫描阶段刚判成不合适的商品被立刻判回合适，两个动作在同一轮里
    一前一后互相抵消。功能看起来完全不生效，而日志和面板都不会报任何异常。
    seller_id 就是这么漏掉过一次的。
    """
    class Spy(dict):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.read = set()

        def get(self, k, d=None):
            self.read.add(k)
            return super().get(k, d)

        def __getitem__(self, k):
            self.read.add(k)
            return super().__getitem__(k)

    # 用一件【完全合规】的商品跑，才能让 judge_snap 一路走到底、把每个字段都读一遍
    spy = Spy(snap(seller_id="123456"))
    assert judge_snap(RULE, spy)["matched"] == 1

    # 【必须先剥掉注释行】这些函数的注释里就写着 "seller_id" 这几个字，
    # 直接拿整段源码做子串判断的话，断言永远为真 —— 测试写完那一刻就是死的。
    # （第一版正是这么写的：把 seller_id 从 SELECT 里删掉，测试照样全绿。）
    for fn, name in _judge_callers():
        sql = _strip_comments(inspect.getsource(fn))
        missing = [f for f in spy.read if f not in sql]
        assert not missing, (
            f"judge_snap 读了 {sorted(spy.read)}，但 {name} 的 SELECT 里没有 {missing} —— "
            "这些字段在那里会是 None，判定结果会和扫描阶段不一致")


def test_explain_读的字段在_全部页_的_SELECT_里都有():
    """「具体原因」列少一个字段不会报错，只会静默退化成兜底文案：
    seller_id 漏掉时每一行都显示「卖家 ? 在黑名单里」，而这一列存在的
    全部意义就是说清楚是哪个条件判掉的。"""
    from web import ui as webui

    class Spy(dict):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.read = set()

        def get(self, k, d=None):
            self.read.add(k)
            return super().get(k, d)

        def __getitem__(self, k):
            self.read.add(k)
            return super().__getitem__(k)

    # all_view 被 @ui.refreshable 包了一层，.func 才是原函数
    sql = _strip_comments(inspect.getsource(webui.all_view.func))
    # explain 对每种 reject_reason 走不同分支，逐个跑一遍才能把字段读全
    for reason in ("", "seller", "excluded_title", "price_over", "price_under",
                   "condition", "shop_item"):
        spy = Spy({"reject_reason": reason, "name": "x", "price": 1, "seller_id": "s",
                   "condition_id": 1, "bid_count": None, "desc_warn": "", "desc_checked": 1})
        explain(RULE, spy)
        missing = [f for f in spy.read if f not in sql]
        assert not missing, (
            f"explain 在 reject_reason={reason!r} 时读了 {missing}，"
            f"但「全部」页的 SELECT 里没有 —— 那一列会静默显示兜底文案")


# ---------------------------------------------------------------- 「全部」页的按钮

def test_全部页按钮三态():
    """插槽模板每行只实例化一份同样的元素，Python 侧没法逐行控制显隐，
    所以文案和可点性必须预先算成行数据。三态各有各的意思：
      —      这一行没有卖家ID（ヤフオク 部分商品不给，メルカリShops 的卖家是店铺）
      已拉黑  已经在该规则的黑名单里了，再点一次只会得到「已经在里面」
      拉黑    可点
    """
    from web.ui import _act_label, _can_blacklist

    rule = {"exclude_sellers": "abc123, p999"}
    assert _act_label({"seller_id": "xyz"}, rule) == "拉黑"
    assert _can_blacklist({"seller_id": "xyz"}, rule) is True

    # 大小写不敏感，和 judge_snap 的比对口径一致
    assert _act_label({"seller_id": "ABC123"}, rule) == "已拉黑"
    assert _can_blacklist({"seller_id": "ABC123"}, rule) is False

    for empty in ("", None, "   "):
        assert _act_label({"seller_id": empty}, rule) == "—"
        assert _can_blacklist({"seller_id": empty}, rule) is False


def test_全部页按钮的判据和_judge_snap_一致():
    """按钮说「已拉黑」而判定却没拉黑（或反过来），面板就在自相矛盾。
    两边都必须走 normalize.ids + 小写精确比对。"""
    from web.ui import _can_blacklist

    rule = dict(RULE)          # exclude_sellers = "917987475, 2yhr98NDVi1eGuLhNbYtU5Z6"
    for sid in ("917987475", "2YHR98ndvI1EgUlHnBytu5z6"):
        judged_out = judge_snap(rule, snap(seller_id=sid))["reject_reason"] == "seller"
        assert judged_out is True
        assert _can_blacklist({"seller_id": sid}, rule) is False, f"{sid} 已被拉黑，按钮不该还能点"
    # 没拉黑的：判定放行，按钮可点
    assert judge_snap(rule, snap(seller_id="ne123"))["matched"] == 1
    assert _can_blacklist({"seller_id": "ne123"}, rule) is True
