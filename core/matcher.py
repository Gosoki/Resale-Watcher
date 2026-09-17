"""规则匹配：给一件商品判「要不要」以及「为什么不要」。

两层设计，对应两种完全不同的处置：

  第 1 层 include_all / include_any 不满足 → keep=False，【不入库】。
      这是「这根本不是我要找的东西」。实测搜 RTX 5090 会返回标题里压根没有 5090 的
      NVIDIA V100（Mercari 的搜索是模糊的），这类噪音入库只会把表撑大。

  第 2 层 其余所有条件不满足 → 入库但 matched=0，并记下 reject_reason。
      这是「是这个东西，但这一件不合适」。必须留痕：价格超区间的明天可能降价，
      而且你按 reject_reason 分组一看就知道自己的规则有没有误杀好货。

reject_reason 的先后顺序是有讲究的：把「永远不会翻身」的原因排在前面。
一件因 excluded_title 被排掉的商品永远不会变合适，而 price_over 的降价后就会。
"""
from core.normalize import ids, norm, word_pairs, words

def judge_snap(rule: dict, snap: dict) -> dict:
    """用搜索结果里的信息做初筛（不含商品描述 —— 那要另发一次详情请求）。

    返回 {'keep', 'matched', 'reject_reason'}。
    """
    title = norm(snap.get("name"))

    inc_all = words(rule.get("include_all"))
    if inc_all and not all(w in title for w in inc_all):
        return {"keep": False, "matched": 0, "reject_reason": "no_keyword"}

    inc_any = words(rule.get("include_any"))
    if inc_any and not any(w in title for w in inc_any):
        return {"keep": False, "matched": 0, "reject_reason": "no_keyword"}

    def reject(reason):
        return {"keep": True, "matched": 0, "reject_reason": reason}

    # 【黑名单排在第 2 层最前面】它是你最明确的主动意图，应该盖过别的原因 ——
    # 拉黑之后在「全部」页看到的却是「标题排除词」，你会以为拉黑没生效。
    seller = (snap.get("seller_id") or "").strip().lower()
    # 【卖家ID未知时不判】和下面品相白名单同一个道理：ヤフオク 有一部分商品不给
    # 卖家ID，メルカリShops 的卖家是店铺不是用户（接口返回哨兵 "0"，已在源那边
    # 归一成空串）。按"不在黑名单里"放行是对的 —— 不知道 ≠ 命中。
    if seller and seller in ids(rule.get("exclude_sellers")):
        return reject("seller")

    if not rule.get("allow_shops") and snap.get("item_type") == "shop":
        return reject("shop_item")

    hit = next((w for w in words(rule.get("exclude_any")) if w in title), None)
    if hit:
        return reject("excluded_title")

    allowed = {int(c) for c in words(rule.get("condition_ids")) if c.isdigit()}
    cond = snap.get("condition_id")
    # 【品相未知时不判】ヤフオク 的搜索结果根本不给品相字段，cond 恒为 None。
    # 若按"不在白名单里"处理，填了 condition_ids 就会把整个 ヤフオク 源静默清零 ——
    # 而你在命中列表里什么都看不到，只会以为那边没货。不知道 ≠ 不符合。
    if allowed and cond is not None and cond not in allowed:
        return reject("condition")

    price = snap.get("price") or 0
    if rule.get("price_min") and price < rule["price_min"]:
        return reject("price_under")
    if rule.get("price_max") and price > rule["price_max"]:
        return reject("price_over")

    return {"keep": True, "matched": 1, "reject_reason": ""}


def flag_desc(rule: dict, description: str | None) -> str:
    """扫描商品描述，返回命中的警示词（逗号分隔）。空串 = 描述干净。

    【只打标签，不否决】商品照样进命中列表，面板上带个黄标写明命中了什么，
    你点开自己判断。这是刻意的取舍：

      误杀是沉默的 —— 被毙掉的商品不会出现在任何列表里，你永远不会知道错过了什么；
      漏筛是可见的 —— 带着黄标进来的坏货，你扫一眼就跳过了。

    【故意不判断否定语境】描述里写「マイニング使用しておらず」（没挖过矿）的，
    照样会挂上「マイニング」标签。曾经为此做过一套日语否定词表 + 逐句切分，
    后来砍了：标签本来就只是「你来看一眼」，误挂一个的代价是你多扫一眼，
    而那套逻辑要额外维护一份否定词表、还要处理句子边界。不值当。

    标签嫌吵的话，直接去面板「规则」页把那个词从 warn_desc 里删掉 ——
    比如「マイニング」在描述里几乎总是以否认形式出现，留着它标签会很密。
    """
    body = norm(description)
    if not body:
        return ""
    hits = [raw for raw, w in word_pairs(rule.get("warn_desc")) if w in body]
    return ",".join(hits)[:128]


def explain(rule: dict, row: dict) -> str:
    """算出「到底是哪个条件把它判成这样的」，给面板「全部」页显示用。

    只负责说明理由，不参与判定 —— 和 judge_snap 读的是同一份词表和同一批阈值，
    所以说出来的原因和实际判定永远一致。调词表时靠它定位：
    看到「excluded_title ← galleria」就知道是哪个词干的，不用去翻代码或跑 replay。
    """
    reason = row.get("reject_reason") or ""

    if reason == "excluded_title":
        title = norm(row.get("name"))
        hits = [raw for raw, w in word_pairs(rule.get("exclude_any")) if w in title]
        return "、".join(hits[:4]) + ("…" if len(hits) > 4 else "")

    if reason == "price_over":
        return f"¥{row['price']:,} ＞ 上限 ¥{rule.get('price_max', 0):,}"

    if reason == "price_under":
        return f"¥{row['price']:,} ＜ 下限 ¥{rule.get('price_min', 0):,}"

    if reason == "condition":
        return f"品相 {row.get('condition_id')}，白名单只有 {rule.get('condition_ids')}"

    if reason == "shop_item":
        return "メルカリShops 商家出品（规则没开「收 Shops」）"

    if reason == "seller":
        return f"卖家 {row.get('seller_id') or '?'} 在黑名单里"

    if reason == "no_keyword":
        # 平时不会入库（第一层就丢弃），只有改严必含词之后重判老商品才会出现
        return f"标题里没有「{rule.get('keyword', '')}」（归一化后比；改了搜索词之后重判出来的）"

    if not reason:
        # 合适的商品：这一列改说还有什么值得看一眼的
        bids = row.get("bid_count")
        if bids:
            return f"🔨 竞价中，已 {bids} 次出价（当前价会涨）"
        warn = row.get("desc_warn") or ""
        if warn == "(描述未读到)":
            return "⚠ 描述没解析出来（页面结构可能变了，警示层对它无效）"
        if warn:
            return f"⚠ 描述含：{warn}"
        if bids == 0:
            buy = row.get("buy_now_price")
            return f"🔨 拍卖，暂无出价" + (f"，一口价 ¥{buy:,}" if buy else "")
        return "✓ 描述已查，干净" if row.get("desc_checked") else "（还没读描述）"

    return ""


def is_deal(price: int, median: int | None, deal_ratio: int,
            deal_price: int = 0) -> tuple[int, int | None]:
    """捡漏判定。返回 (is_deal, deal_pct)。

    【手动价优先，而且不依赖成交样本】deal_price 一旦填了就完全盖过 deal_ratio。
    这正是它存在的理由：百分比那套要先有中位数，而中位数要先攒够成交样本 ——
    规则刚建起来、或者某个型号本来就成交稀少时，百分比整个不工作，
    捡漏徽标永远不亮，而你自己心里其实是有价的。
    pct 照常算（只要有中位数），因为它是给你看的参考，和判定是两件事。

    【判定用未取整的比较，pct 只供显示】原先是先 pct = round(price*100/median)
    再比 pct < deal_ratio，于是「面板宣称的捡漏线」和「真会被判捡漏的价格」差了半个百分点：
    中位数 ¥720,000、deal_ratio=85 时面板写「捡漏线 ¥612,000」，而一件 ¥610,000 的商品
    round(84.72)=85，不小于 85 —— 实际能被判捡漏的最高价是 ¥608,400。
    ¥608,401〜¥612,000 这一段全部卡在「摘要说在线下、后端说不是」的夹缝里。
    改成直接比乘积之后，和 schema.sql 的列注释、规则对话框的 tooltip、面板摘要完全一致。
    """
    # 【用 floor 不用 round】对整数 deal_ratio 有 floor(x) < r ⟺ x < r，
    # 所以显示的百分比和判定结果数学上完全等价 —— 不会出现「写着 85%、
    # 却挂着捡漏徽标」这种看起来自相矛盾的行。round 会。
    pct = int(price * 100 / median) if median else None
    if deal_price:
        return (1 if price < deal_price else 0), pct
    if not median or not deal_ratio:
        return 0, pct          # 没中位数、或关掉百分比判定时仍给 pct，纯做参考
    return (1 if price * 100 < median * deal_ratio else 0), pct
