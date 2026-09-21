"""全局卖家拉黑列表（2026-09-17 之前是每条规则各一份的 exclude_sellers）。

这个功能有六处会静默失效，每一处都是"看起来在跑、其实没生效"：

  1. revalidate 的 SELECT 漏掉 seller_id —— 扫描时判成不合适，紧接着重判又放回来，
     两个动作在同一轮里互相抵消，日志和面板都没有任何异常
  2. メルカリShops 的 sellerId 是哨兵 "0" 而不是真卖家 —— 拉黑"0"会一次误杀
     几十件分属不同店铺的商品，而人以为自己只拉黑了一家
  3. 卖家ID为空时若按"不在白名单里"处理，会把整批不给卖家ID的商品清零
  4. 全局列表的键沿用 exclude_sellers —— watch_rule 里那一列已废弃但永远删不掉
     （sync_schema 只增不删），get_rules 的 SELECT * 带回的空串会把全局列表整个盖掉
  5. 写完库忘了让缓存失效 —— 面板提示「已拉黑」，紧接着的重判读到的还是旧列表，
     商品原地不动，人只会再点一次
  6. 只重判当前那一条规则 —— 别的规则下他的商品在改判之前就被 finalize 推到手机了

下面的用例分别锁这六条。
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import poller  # noqa: E402
from core.matcher import explain, judge_snap  # noqa: E402
from core.normalize import ids  # noqa: E402
from db import store  # noqa: E402
from sources.mercari import _seller  # noqa: E402

RULE = {
    "include_all": "5090", "include_any": "", "exclude_any": "ジャンク", "warn_desc": "",
    # 【用 ids() 构造，不要手写集合】这就是 store.get_settings 建这份集合时的口径：
    # 小写、去空格。测试里另起一套口径的话，两边哪天分叉这里也不会红。
    "blocked_sellers": ids("917987475, 2yhr98NDVi1eGuLhNbYtU5Z6"),
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

def test_拉黑的卖家判不合适但仍入库():
    """入库而不是丢弃：解除拉黑后 revalidate 能把它们重判回命中，不用等重扫。"""
    v = judge_snap(RULE, snap(seller_id="917987475"))
    assert v == {"keep": True, "matched": 0, "reject_reason": "seller"}


def test_不在列表里的卖家正常命中():
    assert judge_snap(RULE, snap(seller_id="123456"))["matched"] == 1


def test_大小写不敏感():
    """ヤフオク 的 ID 是混合大小写的，手抄时大小写出入不该导致拉黑失效。"""
    assert judge_snap(RULE, snap(seller_id="2YHR98ndvI1EgUlHnBytu5z6"))["reject_reason"] == "seller"


def test_拉黑优先于标题排除词():
    """拉黑之后在「全部」页却看到「标题排除词」，人会以为拉黑没生效。"""
    v = judge_snap(RULE, snap(seller_id="917987475", name="RTX 5090 ジャンク"))
    assert v["reject_reason"] == "seller"


# ---------------------------------------------------------------- 全局：这次改动的全部意义

def test_一次拉黑对所有规则生效():
    """【这是这次改动的全部意义】原先是每条规则各一份名单：同一个刷屏的店铺
    要在每条规则里分别拉一次，漏掉一条，他的商品照样从那条规则推到你手机上。

    实装成"只并进当前这条规则"的话，面板照样弹「已拉黑」，而另一条规则下
    什么都没变 —— 没有任何报错，你只会在几分钟后收到他的推送。
    """
    blocked = ids("917987475")
    单卡 = {"include_all": "5090", "exclude_any": "ゲーミングpc", "allow_shops": 1,
            "blocked_sellers": blocked}
    整机 = {"include_all": "5090", "exclude_any": "", "allow_shops": 1,
            "condition_ids": "1,2,3", "blocked_sellers": blocked}
    for rule in (单卡, 整机):
        assert judge_snap(rule, snap(seller_id="917987475"))["reject_reason"] == "seller"


def test_拉黑列表必须真的到达规则字典():
    """【第 4 条防线】judge_snap 不认识 store，列表是 _with_settings 并进去的。
    那一行忘了改（或者 merge 时丢了），上面所有用例照样全绿、面板照样弹
    「已拉黑」，而全站一个卖家都拦不住 —— 因为 rule.get() 拿到 None 就是放行。
    """
    saved_q, saved_cache = store.query, dict(store._settings_cache)
    try:
        store.query = lambda sql, args=(): (
            [{"seller_id": "Ab1"}] if "blocked_seller" in sql else [])
        store.get_settings(force=True)
        rule = store._with_settings({"id": 1, "name": "x"})
        assert isinstance(rule["blocked_sellers"], frozenset), "必须是集合，理由见下一条"
        assert rule["blocked_sellers"] == frozenset({"ab1"}), "没并进来，或者没小写"
    finally:
        store.query = saved_q
        store._settings_cache.clear()
        store._settings_cache.update(saved_cache)


def test_全局键不许和watch_rule的列同名():
    """_with_settings 是 {**全局, **规则}：规则自身的字段优先。哪天有人往
    watch_rule 里加一列叫 blocked_sellers，SELECT * 带回的空串会把全局集合
    整个盖掉 —— 判定全部放行，毫无征兆。这也是没有沿用 exclude_sellers 的原因。
    """
    assert "blocked_sellers" not in store.RULE_FIELDS
    assert "exclude_sellers" not in store.RULE_FIELDS, "已废弃的列不该再被读写"


def test_列表必须是集合不能是逗号串():
    """字符串的 in 是【子串匹配】："91798747" in "917987475" 为真 ——
    拉黑一个人会连坐一批ID里恰好含它的卖家，而他们只是从此不再出现。"""
    rule = dict(RULE, blocked_sellers=frozenset({"917987475"}))
    assert judge_snap(rule, snap(seller_id="91798747"))["matched"] == 1


def test_跨源不误杀():
    """并成一份全局列表之后【新出现】的风险面：三个源的ID体系互不重叠
    （Mercari 9 位数字 / Yahoo!フリマ p+数字 / ヤフオク 28~29 位 base62），
    所以单列主键是安全的 —— 这条用例就是那个前提的看门人。"""
    rule = dict(RULE, blocked_sellers=frozenset({"917987475"}))
    for sid in ("p58365705", "2yhr98NDVi1eGuLhNbYtU5Z6mWntA", "917987475x"):
        assert judge_snap(rule, snap(seller_id=sid))["matched"] == 1, sid


# ---------------------------------------------------------------- 卖家未知

def test_卖家ID为空时一律放行():
    """ヤフオク 有一部分商品不给卖家ID。按"不在白名单里"处理会把它们整批清零，
    而你在命中页什么都看不到，只会以为那边没货。不知道 ≠ 命中。

    【先断言列表非空】否则这一条会在"列表根本没送到"时静默变绿 ——
    那时 matched==1 恒成立，而它恰恰是「宁可漏筛不可误杀」的唯一防线。
    """
    assert RULE["blocked_sellers"], "列表是空的，这条用例证明不了任何事"
    assert judge_snap(RULE, snap(seller_id=""))["matched"] == 1
    assert judge_snap(RULE, snap(seller_id=None))["matched"] == 1


def test_个人出品取_sellerId():
    assert _seller({"sellerId": "917987475"}) == "917987475"


def test_メルカリShops_取店铺ID而不是哨兵_0():
    """Shops 的卖家是店铺实体不是用户，接口对它们一律返回 sellerId="0"。
    实测库里 41 件顶着这个"卖家ID"、分属不同店铺 —— 拿 "0" 当卖家ID 的话，
    列表里填一个 0 会一次误杀这 41 件；归一成空串则让它们【没法拉黑】，
    而"店铺反复挂高价货刷屏"正是拉黑最主要的用途。真正的标识是 shop.id。"""
    shops = {"sellerId": "0", "shop": {"id": "YoUQXX6TT8X6C47LxjRZbH"},
             "itemType": "ITEM_TYPE_BEYOND"}
    assert _seller(shops) == "YoUQXX6TT8X6C47LxjRZbH"
    # 拉黑这家店 → 它的商品在【每一条】规则下都判为不合适
    blocked = ids("YoUQXX6TT8X6C47LxjRZbH")
    for rule in (dict(RULE, blocked_sellers=blocked),
                 {"include_all": "", "allow_shops": 1, "blocked_sellers": blocked}):
        assert judge_snap(rule, snap(seller_id=_seller(shops)))["reject_reason"] == "seller"


def test_既没有_sellerId_也没有_shop_时才算未知():
    assert RULE["blocked_sellers"], "列表是空的，这条用例证明不了任何事"
    for raw in ({}, {"sellerId": ""}, {"sellerId": "0"}, {"sellerId": "0", "shop": None},
                {"sellerId": None, "shop": {}}):
        assert _seller(raw) == "", raw
        assert judge_snap(RULE, snap(seller_id=_seller(raw)))["matched"] == 1


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


# ---------------------------------------------------------------- 缓存

def test_读列表失败时沿用上一份而不是清空():
    """【第 5 条防线的反面】连接层刻意不自动重试（见 db/store.py 开头）：
    服务端在两次 ping 之间掐掉连接时，第一条查询必然抛一次、第二条就好了。

    这里要是像读设置那样回落成空集合，那一轮 revalidate 会把【所有】被拉黑的
    商品判回 matched=1，而它们 notified_at IS NULL —— 紧接着的 push_new 就把
    你亲手拉黑的那个人一串推到手机上，10 秒后又静默判回去，日志里一条线索都没有。
    """
    saved_q, saved_cache = store.query, dict(store._settings_cache)
    try:
        store.query = lambda sql, args=(): (
            [{"seller_id": "abc"}] if "blocked_seller" in sql else [])
        assert store.get_settings(force=True)["blocked_sellers"] == frozenset({"abc"})

        def boom(sql, args=()):
            if "blocked_seller" in sql:
                raise RuntimeError("连接被服务端回收")
            return []

        store.query = boom
        assert store.get_settings(force=True)["blocked_sellers"] == frozenset({"abc"}), \
            "读失败时把列表清空了 —— 等于全体静默解除拉黑"
    finally:
        store.query = saved_q
        store._settings_cache.clear()
        store._settings_cache.update(saved_cache)


def test_写完库要让缓存立刻失效():
    """不失效的话，面板点完拉黑、紧接着的同步重判读到的还是旧列表：
    提示写着「已拉黑」而一件都没改判，商品原地不动，人只会再点一次。"""
    for fn in (store.block_seller, store.unblock_seller, store.save_setting):
        assert "_drop_settings_cache()" in _strip_comments(inspect.getsource(fn)), \
            f"{fn.__name__} 写完库没让缓存失效"


def test_失效函数本身真的会让下一次读重新查库():
    """【上面那条只盯着调用点，盯不住这个函数自己】把函数体改成
    `at = time.monotonic()`（看起来很像"刷新时间戳"）或者直接 pass，
    三个调用点的字符串都还在、上面那条照样绿，而拉黑当场变成 10 秒内的空操作。
    """
    import time

    hits = []
    saved_q, saved_cache = store.query, dict(store._settings_cache)
    try:
        store.query = lambda sql, args=(): (hits.append(sql), [])[1]
        store._settings_cache.update(at=time.monotonic(), data={"blocked_sellers": frozenset()})
        store.get_settings()
        assert hits == [], "缓存没热起来，这条用例的前提就不成立"
        store._drop_settings_cache()
        store.get_settings()
        assert hits, "失效之后不带 force 的读还是命中了缓存 —— 拉黑在 10 秒内是空操作"
    finally:
        store.query = saved_q
        store._settings_cache.clear()
        store._settings_cache.update(saved_cache)


# ---------------------------------------------------------------- 推送侧的硬保证

def test_两条待推查询都排除了拉黑的卖家():
    """【判定挡不住跨进程的那 10 秒】另一台机器手里的列表最多旧 10 秒，窗口里
    它会把 matched=1 写回去，而那些行 notified_at IS NULL，下一步就是推送。
    把条件写死在 SQL 里，推送从此和缓存新鲜度、和两个进程的时序完全无关。
    """
    seen = []
    saved = store.query
    store.query = lambda sql, args=(): (seen.append(" ".join(sql.split())), [])[1]
    try:
        store.pending_notify(1, False)
        store.pending_final(1, 60)
    finally:
        store.query = saved
    assert len(seen) == 2
    for sql in seen:
        assert "NOT EXISTS" in sql and "blocked_seller" in sql, sql
        # 【这一句不能省】万一表里混进一行空串，少了它会把所有"卖家未知"的商品
        # 一次性静默停推 —— ヤフオク 有一批就是不给卖家ID的。
        assert "item.seller_id <> ''" in sql, sql


def test_提示里的件数按链接算_不数item的行数():
    """item 的主键是 (source, item_id, rule_id)：同一个链接被 N 条规则抓到就是 N 行。
    实测有个卖家 22 行、其实只有 10 件商品 —— 直接 COUNT(*) 会把提示写成
    「他名下 22 件商品」，而这个函数存在的唯一理由就是给人看一个真实的数字。"""
    seen = []
    saved = store.one
    store.one = lambda sql, args=(): (seen.append(" ".join(sql.split())), {"n": 0})[1]
    try:
        store.seller_item_count("x")
    finally:
        store.one = saved
    assert "COUNT(DISTINCT source, item_id)" in seen[0], seen


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


def test_拉黑之后要重判全部规则():
    """【第 6 条防线】只重判当前这条的话，别的规则下他的商品在改判之前就已经
    被 finalize 推出去了 —— 而拉黑的意思正是"哪条规则下都别再给我看到他"。"""
    src = _strip_comments(inspect.getsource(poller.revalidate_all))
    assert "store.get_rules()" in src, "必须遍历全部规则"
    # 【比的是调用写法，不是"源码里出现过 enabled_only"】docstring 里就解释着
    # 为什么不带它，拿整段做子串判断的话这一条写完就是死的。
    assert "get_rules(enabled_only" not in src, (
        "停用的规则也要重判：跳过它等于它名下的判定理由永远停在旧值，"
        "哪天重新启用还要再等一轮")
    from web import ui as webui
    for fn in (webui.blacklist_seller, webui.unblock_seller, webui.add_blocked):
        assert "revalidate_all()" in _strip_comments(inspect.getsource(fn)), \
            f"{fn.__name__} 只重判了一条规则"


def test_explain_读的字段在_全部页_的_SELECT_里都有():
    """「具体原因」列少一个字段不会报错，只会静默退化成兜底文案：
    seller_id 漏掉时每一行都显示「卖家 ? 在拉黑列表里」，而这一列存在的
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


# ---------------------------------------------------------------- 面板

def test_全部页按钮三态():
    """插槽模板每行只实例化一份同样的元素，Python 侧没法逐行控制显隐，
    所以文案和可点性必须预先算成行数据。三态各有各的意思：
      —      这一行没有卖家ID（ヤフオク 部分商品不给，メルカリShops 的卖家是店铺）
      已拉黑  已经在拉黑列表里了，再点一次只会得到「已经在里面」
      拉黑    可点
    """
    from web.ui import _act_label, _can_blacklist

    blocked = ids("abc123, p999")
    assert _act_label({"seller_id": "xyz"}, blocked) == "拉黑"
    assert _can_blacklist({"seller_id": "xyz"}, blocked) is True

    # 大小写不敏感，和 judge_snap 的比对口径一致
    assert _act_label({"seller_id": "ABC123"}, blocked) == "已拉黑"
    assert _can_blacklist({"seller_id": "ABC123"}, blocked) is False

    for empty in ("", None, "   "):
        assert _act_label({"seller_id": empty}, blocked) == "—"
        assert _can_blacklist({"seller_id": empty}, blocked) is False


def test_全部页按钮的判据和_judge_snap_一致():
    """按钮说「已拉黑」而判定却没拉黑（或反过来），面板就在自相矛盾。
    两边读的必须是同一份集合、同一个小写精确比对。"""
    from web.ui import _can_blacklist

    blocked = RULE["blocked_sellers"]
    for sid in ("917987475", "2YHR98ndvI1EgUlHnBytu5z6"):
        judged_out = judge_snap(RULE, snap(seller_id=sid))["reject_reason"] == "seller"
        assert judged_out is True
        assert _can_blacklist({"seller_id": sid}, blocked) is False, f"{sid} 已拉黑，按钮不该还能点"
    # 没拉黑的：判定放行，按钮可点
    assert judge_snap(RULE, snap(seller_id="ne123"))["matched"] == 1
    assert _can_blacklist({"seller_id": "ne123"}, blocked) is True


# ---------------------------------------------------------------- 手动添加

def _added(text, have=frozenset()):
    """跑一遍 add_blocked，返回真正落进 blocked_seller 的那些串。"""
    from web import ui as webui

    got = []
    saved = (store.block_seller, store.blocked_sellers, poller.revalidate_all,
             webui.notify, webui._refresh_after_block)
    store.block_seller = lambda sid, row=None: got.append(sid)
    store.blocked_sellers = lambda: frozenset(have)
    poller.revalidate_all = lambda: 0
    webui.notify = lambda msg, **k: _added.notes.append(k.get("type"))
    # 重画三个页要有活着的 NiceGUI 事件循环，这条用例只关心"落库的是哪个串"
    webui._refresh_after_block = lambda: None
    _added.notes = []
    try:
        webui.add_blocked(type("Inp", (), {"value": text})())
    finally:
        (store.block_seller, store.blocked_sellers, poller.revalidate_all,
         webui.notify, webui._refresh_after_block) = saved
    return got


def test_手动添加原样入库_只拿小写去重():
    """【判定不受影响，坏的是那串字符本身】ヤフオク 和 メルカリShops 的ID 是
    大小写敏感的 base62。压成小写照样拦得住人（两边都小写），但列表里显示的
    那串就复制不回源站打开了 —— 而手动加的行没有商品链接，那串ID是唯一的把手。
    """
    from core.normalize import id_tokens, ids

    raw = "2yhr98NDVi1eGuLhNbYtU5Z6, YoUQXX6TT8X6C47LxjRZbH"
    assert id_tokens(raw) == ["2yhr98NDVi1eGuLhNbYtU5Z6", "YoUQXX6TT8X6C47LxjRZbH"]
    # ids() 仍然是比对口径（小写），两者必须拆得一样多
    assert len(ids(raw)) == len(id_tokens(raw))

    # 【真跑一遍 add_blocked，不是断言源码长什么样】看落库的到底是哪个串
    assert _added(raw) == ["2yhr98NDVi1eGuLhNbYtU5Z6", "YoUQXX6TT8X6C47LxjRZbH"]
    # 已经在列表里的那个（口径是小写）不该被重复加
    assert _added("2YHR98ndvI1EgUlHnBytu5z6", have={"2yhr98ndvi1egulhnbytu5z6"}) == []


def test_手动粘一整条网址要被拦住而不是静默截断():
    """列宽是 VARCHAR(32)，超长的落库会被截成半截：拦不住任何商品，而下次再粘
    同一条时「已在列表里」又比不中（库里是截断后的）—— 于是每次都提示
    「已拉黑 1 人」、每次都白跑一遍全局重判，永远收敛不了，日志里毫无线索。
    """
    assert _added("https://jp.mercari.com/user/profile/967376952") == [], "一个都不该落库"
    assert _added.notes == ["negative"], "粘网址应该出声拒绝，而不是静默截断"
    # 正常的ID 不受影响
    assert _added("967376952") == ["967376952"]


def test_面板上必须有解除的入口():
    """【没有这一块就不该做拉黑这个功能】范式同「已剔除」：误点一下就再也
    找不回来的话，这个按钮本身就是个陷阱。列表在规则页最下面，两处拉黑按钮的
    tooltip 都指到那里。"""
    from web import ui as webui

    assert "_blocked_block()" in _strip_comments(inspect.getsource(webui.rules_view.func)), \
        "规则页没画拉黑列表 —— 拉黑之后没有任何地方能看到、能解除"
    src = inspect.getsource(webui._blocked_block)
    assert "解除" in src and "unblock_seller" in src
    # 手动添加的入口也不能少：拉黑按钮只在有卖家ID的行上有，而规则对话框里
    # 原本那个 exclude_sellers 文本框已经删了。
    assert "add_blocked" in src
