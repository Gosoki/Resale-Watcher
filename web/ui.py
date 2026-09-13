"""NiceGUI 面板：改规则、看命中、调排除词。

四个页签各对应一件事：
  命中  —— 平时只看这一页：通过全部规则的在售商品，按规则折叠，
            组内按「市价百分比」升序（最划算的排最前）
  全部  —— 调规则时看这一页：一张平铺表，可按规则/判定原因筛选，
            「具体原因」列会告诉你每一件是被哪个词判掉的
  规则  —— 你自己填的那张表，以及每条规则在各个源上的轮询状态
  设置  —— 全局项（抓取节奏、市价样本窗口、每日上限），改完 10 秒生效
"""
from datetime import timedelta

from nicegui import run, ui

import config
import sources
from core import poller
from core.matcher import explain
from core.normalize import ids
from db import store

# 暗色样式：ui.dark_mode(True) + ui.colors 定主色 + 这张表修 Quasar 在暗底上的两处短板。
DARK_CSS = (
    "<style>"
    "body{font-size:14px}"
    # Quasar 的卡片/表格默认带投影，那是给亮底设计的；暗底上投影看不见，只会糊成一团。
    # 换成极淡的白色描边来分层。
    ".q-card{box-shadow:none!important;border:1px solid rgba(255,255,255,.08)}"
    ".q-table__container,.q-table__card,.q-table{box-shadow:none!important}"
    ".q-table tbody td,.q-table thead th{font-size:14px}"
    # 命中列表每行之间的分隔线：Tailwind 的 border-t 默认取亮底灰，暗底上几乎是白的
    ".border-t{border-top:1px solid rgba(255,255,255,.08)!important}"
    "@layer overrides{"
    # 徽标背景统一到 Tailwind -400 档（和文字色同一套），免得 Quasar 原色在暗底上过饱和
    ".q-badge.bg-green{background:oklch(72.3% 0.219 149.579)!important}"     # green-500
    ".q-badge.bg-orange{background:oklch(75% 0.183 55.934)!important}"       # orange-400
    ".q-badge.bg-amber{background:oklch(82.8% 0.189 84.429)!important}"      # amber-400
    ".q-badge.bg-grey{background:oklch(70.7% 0.022 261.325)!important}"      # gray-400
    ".q-badge.bg-teal{background:oklch(77.7% 0.152 181.912)!important}"      # teal-400
    # 【这半条和上面是同一个决定】Quasar 的 .q-badge{color:#fff} 是写死的白字，
    # 而上面这批背景亮度在 70%~83% —— amber 上白字只有 1.7:1、green 2.2:1，全线看不清。
    # 亮底一律改配深色前景。
    ".q-badge.bg-green,.q-badge.bg-orange,.q-badge.bg-amber,.q-badge.bg-grey,"
    ".q-badge.bg-teal{color:#18181b!important}"
    "}"
    "</style>"
)

# 发货地标签只标这一个 —— 你要的是「在不在东京都内」，其余都道府县
# 照常显示在下面那行小字里，但不占徽标位。
TOKYO = "東京都"

COND = {1: "新品未使用", 2: "未使用に近い", 3: "傷汚れなし",
        4: "やや傷汚れ", 5: "傷や汚れあり", 6: "状態が悪い"}

REASON_LABEL = {
    "": "合适", "price_over": "超预算", "price_under": "低于下限",
    "excluded_title": "标题排除词",
    # no_keyword 平时不会入库（第一层判定直接丢弃），但 poller.revalidate 会用当前规则
    # 重判【已在库】的商品 —— 你把必含词改严之后，老商品就会被写成这个原因留在库里
    "no_keyword": "关键词不匹配",
    "condition": "品相不符", "shop_item": "Shops商家品",
    "seller": "卖家黑名单",
}

# 编辑对话框里每个字段的提示。写在这里而不是只靠 DDL 注释 ——
# 填表的人在面板上，不会去翻 SHOW FULL COLUMNS。
FIELD_HELP = {
    "name": "规则名，只给人看",
    "keyword": "搜索词，发给所有启用的数据源。宁可宽一点，精筛交给下面的词表",
    "include_all": "必含词（全部要有）。填「5090」就能命中 RTX5090 / RTX 5090 / ＲＴＸ５０９０",
    "include_any": "任含词（有一个就行）。留空=不检查",
    "exclude_any": "标题排除词，命中任一即判不合适。整机靠 CPU 型号（ryzen/ultra9/14900）最好认",
    "warn_desc": "描述警示词，只查描述。【命中只打黄标，不会毙掉商品】所以可以放宽一点填，"
                 "宁可多挂个标签让你看一眼，也别静悄悄漏掉一块好卡",
    "exclude_sellers": "卖家黑名单：逗号分隔的卖家ID，命中就判不合适。"
                       "【不用手抄】命中页每行有「拉黑卖家」按钮，点一下就加进来。"
                       "取消拉黑（从这里删掉）后，被误杀的商品下一轮会自己回到命中列表",
    "condition_ids": "品相白名单 1新品〜6状态差，逗号分隔。留空=不限",
    "note": "备注",
}


def notify(message: str, **kw) -> None:
    """弹通知。带 type 的一律改深色前景。

    Quasar 给 positive/warning/info 配的底色都是亮的（warning #F2C037、positive #21BA45），
    而它写死配白字 —— 实测只有 1.9~2.9:1，看不清。negative 也一样：上面 ui.colors 把它
    定成了 red-400，同样是亮底。不带 type 的通知【不能】动，那时底色是 Quasar 默认的
    #323232 深灰，改成深字反而只剩 1.6:1。

    【必须写驼峰 textColor】NiceGUI 只把 close_button/multi_line 转驼峰（见 ARG_MAP），
    其余原样下发给 Quasar；写成 text_color 会被它当不认识的选项静默忽略 ——
    看起来改了、实际什么都没发生。
    """
    if kw.get("type") in ("positive", "negative", "warning", "info"):
        kw.setdefault("textColor", "dark")
    ui.notify(message, **kw)


def item_url(source: str, item_id: str) -> str:
    """商品页地址由各数据源自己给 —— 路径规则各家不一样，别在这里拼。"""
    src = sources.get(source)
    return src.item_url(item_id) if src else "#"


def source_name(key: str) -> str:
    src = sources.get(key)
    return src.name if src else key


def yen(n) -> str:
    return f"¥{n:,}" if n is not None else "—"


def time_left(end) -> str:
    """距结束还有多久。拍卖里这是最要紧的一个数 —— 到点就没了。"""
    if not end:
        return ""
    sec = (end - config.now()).total_seconds()
    if sec <= 0:
        return "已结束"
    h = int(sec // 3600)
    if h < 1:
        return f"剩 {int(sec // 60)} 分"
    if h < 24:
        return f"剩 {h} 小时"
    return f"剩 {h // 24} 天 {h % 24} 小时"


def freshness(r: dict, hours: int, cold_start: bool) -> str:
    """返回 "listed"（新上架）/ "found"（新发现）/ ""（都不是）。

    【为什么要分两个】原先只有一个「新」标，判的是 first_seen_at —— 也就是
    「我们什么时候抓到的」，而不是「商品什么时候挂出来的」。实测这两个差得离谱：
    Mercari 的商品平均滞后 185 天，最久的一件上架 1405 天后我们才第一次抓到它。
    于是一件挂了三年多的商品会被标成「新」，标签完全没有信息量。

      新上架  商品本身刚挂出来（用平台给的 listed_at）—— 你直觉理解的「新」
      新发现  商品早就挂着，但今天才进我们的库。多半是它降价进了你的价格区间，
              对你来说同样是新机会，只是性质不同，不该和上面混为一谈

    ヤフオク 的搜索结果不给上架时间，它的商品只可能是「新发现」。
    """
    win = timedelta(hours=hours)
    now = config.now()

    # 【新上架】判的是平台给的上架时间，和我们什么时候开始监控毫无关系 ——
    # 所以冷启动【不】抑制它。之前把它也一起抑制了，结果规则刚建起来的头两天
    # （fresh_hours 调到 48 就是两天）整页一个徽标都看不到，正是最想看的时候。
    if r["listed_at"] and now - r["listed_at"] < win:
        return "listed"

    # 【新发现】判的是"我们刚抓到"。规则刚开始监控时库里所有东西都是刚抓到的，
    # 这个标会糊满整页，那才是真的没有信息量 —— 只抑制它。
    if cold_start:
        return ""
    if now - r["first_seen_at"] < win:
        return "found"
    return ""


def auction_note(r: dict) -> tuple[str, str]:
    """拍卖商品的提示文字和颜色。返回 ("", "") 表示这不是拍卖。

    【为什么必须显示】判定用的是【当前价】——而拍卖的当前价只在此刻成立。
    一件已有 10 次出价的卡，当前价落在你预算内不代表你买得到。
    不把出价数和剩余时间摆出来，"命中"两个字就是在误导人。
    """
    if r.get("bid_count") is None:
        return "", ""
    bids = r["bid_count"]
    left = time_left(r.get("end_time"))
    urgent = r.get("end_time") and (r["end_time"] - config.now()).total_seconds() < 3600
    if bids > 0:
        return f"🔨 竞价中 · 已 {bids} 次出价 · 当前价还会涨 · {left}", \
               ("text-red-400" if urgent else "text-orange-400")
    buy = r.get("buy_now_price")
    tail = f" · 一口价 {yen(buy)}" if buy else " · 无一口价，只能竞价"
    return f"🔨 拍卖 · 暂无人出价{tail} · {left}", \
           ("text-red-400" if urgent else "text-gray-400")


def blacklist_seller(rule_id: int, seller_id: str) -> None:
    """把这个卖家加进该规则的黑名单，并【立刻】重判一次。

    立刻重判（而不是等下一轮）是因为这是个手动动作：点完按钮商品还挂在页面上，
    人会以为没生效、然后再点一次。revalidate 是纯 CPU、不发任何请求，
    在这里同步跑一次的代价只是几百行 UPDATE。
    """
    rule = store.get_rule(rule_id)
    if not rule:
        notify("这条规则不在了（可能刚被删掉）", type="warning")
        return
    cur = rule["exclude_sellers"] or ""
    if seller_id.strip().lower() in ids(cur):
        notify(f"{seller_id} 已经在黑名单里了")
        return
    new = f"{cur},{seller_id}" if cur else seller_id
    # exclude_sellers 是 VARCHAR(1024)。满了必须出声 —— 非严格模式下 MySQL 会
    # 【静默截断】，那会把最后一个 ID 砍成半截：既没拉黑成，又可能误伤一个
    # 前缀恰好相同的卖家，而面板上显示的是「已拉黑」。
    if len(new) > 1024:
        notify("黑名单满了（上限 1024 字符）—— 去规则页删掉几个不再需要的 ID",
               type="negative")
        return
    store.update_rule(rule_id, {"exclude_sellers": new})
    n = poller.revalidate(store.get_rule(rule_id))
    # 【提示里要点名是哪条规则】黑名单是按规则存的，而「全部」页可以同时列出
    # 多条规则的商品 —— 不说清楚的话，你以为拉黑了这个人，其实只在一条规则里生效。
    notify(f"「{rule['name']}」已拉黑 {seller_id}，{n} 件商品改判")
    # 两个页都要刷：从哪个页点的都可能。refresh() 不带参数会沿用各自最近一次的
    # 参数（NiceGUI 的 target.args = args or target.args），所以「全部」页的
    # 筛选条件不会被重置回「全部规则」。没渲染过的页 targets 为空，是空操作。
    hits_view.refresh()
    all_view.refresh()


# ------------------------------------------------------------------ 命中页

@ui.refreshable
def hits_view() -> None:
    rules = store.get_rules()
    if not rules:
        ui.label("还没有规则，去「规则」页新建一条。").classes("text-gray-400 p-4")
        return

    fresh_hours = store.get_settings()["fresh_hours"]
    for rule in rules:
        st = store.get_state(rule["id"])
        med = st["median_price"]
        # 冷启动判断：这条规则库里【最早】的商品也是刚抓到的，说明监控本身才刚开始
        earliest = store.one("SELECT MIN(first_seen_at) m FROM item WHERE rule_id = %s",
                             (rule["id"],))["m"]
        cold_start = bool(earliest) and (config.now() - earliest) < timedelta(hours=fresh_hours)
        rows = store.query(
            "SELECT * FROM item WHERE rule_id = %s AND matched = 1 AND status = 'on_sale' "
            "ORDER BY COALESCE(deal_pct, 999), price", (rule["id"],))

        # 折叠摘要：折起来之后这一行就是你能看到的全部，所以预算/市价/捡漏线都要在里面
        summary = [f"预算 {yen(rule['price_min'])}〜{yen(rule['price_max'])}"]
        if med:
            summary.append(f"市价中位 {yen(med)}（{st['sample_count']}件成交）")
        else:
            summary.append(f"成交样本不足{rule['median_min_samples']}件，暂无市价参考")
        # 【手动价填了就盖过百分比】摘要必须如实反映当前生效的那一个，
        # 否则你会对着「低于 ¥706,500 算捡漏」纳闷为什么 ¥70 万的没标捡漏。
        if rule["deal_price"]:
            summary.append(f"手动捡漏价 {yen(rule['deal_price'])}")
        elif med and rule["deal_ratio"]:
            summary.append(f"低于 {yen(med * rule['deal_ratio'] // 100)} 算捡漏")
        deals = sum(1 for r in rows if r["is_deal"])
        head = f"{rule['name']}　{len(rows)} 件" + (f"　🟢 {deals} 件捡漏" if deals else "")

        # value=True＝默认展开。折叠状态只活在当前页面里，刷新后回到展开 ——
        # 这正是想要的：命中列表是每次打开都要从头扫一遍的东西。
        with ui.expansion(head, caption=" · ".join(summary), value=True) \
                .classes("w-full mb-3").props("header-class=text-base"):
            # 【放在「没有商品」判断之前】一件都没命中的时候，恰恰是你最想调这个价的时候。
            with ui.row().classes("items-center gap-2 mb-2 flex-wrap"):
                ui.label("手动捡漏价 ¥").classes("text-xs text-gray-400")
                # 【必须用默认参数绑死 rid/comp】这两个是循环变量，直接引用的话
                # 等你点保存时它们早就指向最后一条规则了 —— 每个按钮都会改同一条。
                inp = ui.number(value=rule["deal_price"] or None, format="%d") \
                    .props("dense outlined").classes("w-40")
                ui.button("保存", on_click=lambda _, rid=rule["id"], c=inp:
                          save_deal_price(rid, c.value)) \
                    .props("flat dense no-caps size=sm color=primary")
                ui.label("低于它就算捡漏，填了就盖过下面的百分比；留空或 0 = 回到按成交中位数判"
                         ).classes("text-xs text-gray-400")
            if not rows:
                ui.label("当前没有符合条件的在售商品。").classes("text-gray-400 text-sm")
                continue

            # 【宽屏两列】一件商品那一行最窄要 ~480px 才不挤（图 96 + 正文 + 价格列），
            # xl 是 80rem=1280px，两列各约 610px，够。再窄就退回一列 ——
            # 不设下限的话，笔记本上正文会被压到标题每行只剩几个字。
            # gap 只给 x 方向：纵向的间距由每行自己的 border-t + py-2 负责，
            # 再加 gap-y 会让两列之间的分隔线对不齐。
            with ui.element("div").classes("grid grid-cols-1 xl:grid-cols-2 gap-x-6 w-full"):
                for r in rows:
                    fresh = freshness(r, fresh_hours, cold_start)
                    # 【这一行的三个 class 是一组，缺一个价格就会被长标题挤下去】
                    #   flex-nowrap  外层三列（图/正文/价格）绝不换行 —— 没有它，
                    #                标题一长整个价格列会被挤到下一行去
                    #   items-start  标题换成两行时，价格保持在顶部对齐而不是浮到中间
                    #   正文列的 min-w-0 + 价格列的 shrink-0 见下面，是同一件事的另一半：
                    #   flex 子项默认 min-width:auto，不写 min-w-0 的话正文列会被内容
                    #   撑到超过容器宽度，把右边挤没
                    with ui.row().classes("items-start w-full gap-3 border-t py-2 flex-nowrap"):
                        if r["thumb_url"]:
                            # 96px：正文列在「徽标+标题两行+拍卖提示+品相行」时约 90px 高，
                            # 图跟着长到差不多，两边才齐。64px 时右边明显空一块，
                            # 看起来就像行距被撑开了。
                            ui.image(r["thumb_url"]).classes(
                                "w-24 h-24 object-cover rounded shrink-0")
                        # leading-snug：正文是 3~4 行小字堆起来的，默认行高留白偏多，
                        # 累积下来整张卡片会显得松垮
                        with ui.column().classes("gap-0 grow min-w-0 leading-snug"):
                            # 【徽标在标题上方】它们是"要不要点进去"的信号，得一眼看见。
                            # 放在标题后面的话，遇到长标题（全库最长 130 字，超 70 字的有
                            # 一百多件）就会被推到第二三行的行尾，等于没有。
                            # 没有任何徽标时整行不渲染，不留空档。
                            if (r["is_deal"] or fresh or r["desc_warn"]
                                    or r["ship_from"] == TOKYO):
                                with ui.row().classes("items-center gap-2 flex-wrap mb-1"):
                                    if r["is_deal"]:
                                        ui.badge("捡漏", color="green")
                                    if r["ship_from"] == TOKYO:
                                        ui.badge(TOKYO, color="teal").tooltip(
                                            "发货地在东京都内。【只有拉过详情的商品才知道发货地】"
                                            "三个源都只在详情里给这个字段，搜索结果里没有——"
                                            "没这个标不等于不在东京，可能只是还没拉详情")
                                    if fresh == "listed":
                                        ui.badge("新上架", color="orange").tooltip(
                                            f"平台显示它是最近 {fresh_hours} 小时内挂出来的")
                                    elif fresh == "found":
                                        ui.badge("新发现", color="blue-grey").tooltip(
                                            f"最近 {fresh_hours} 小时内才进我们的库。商品本身可能"
                                            "早就挂着了 —— 多半是它降价进了你的价格区间。"
                                            "ヤフオク 不提供上架时间，它的商品只会有这个标")
                                    if r["desc_warn"] == poller.DESC_UNREAD:
                                        # 【这不是警示词，是降级信号】详情页打开了但描述没解析出来
                                        # （多半是平台改版）。画成普通警示徽标、还配一句
                                        # 「可能是卖家在否认」的解释，等于用假信息盖住了
                                        # 「警示层对这件商品整个失效」这个事实。
                                        ui.badge("描述未读到", color="grey").tooltip(
                                            "详情页打开了，但描述没解析出来（平台页面结构可能变了）。"
                                            "这件商品的描述警示【没有生效】，点进去自己看一眼")
                                    elif r["desc_warn"]:
                                        # 描述里命中了警示词。商品没被毙掉，只是提醒你点开看一眼
                                        ui.badge(f"描述: {r['desc_warn']}", color="amber") \
                                            .tooltip("描述里出现了这些词，但可能是卖家在否认（如"
                                                     "「ジャンク品ではありません」）。点标题自己看一眼")
                            # 标题【不截断】，长了就换行（break-words 让超长的连续
                            # 字符串——比如日文长串型号——也能断开，不会撑破容器）
                            ui.link(r["name"], item_url(r["source"], r["item_id"]),
                                    new_tab=True).classes("font-medium break-words")
                            note, color = auction_note(r)
                            if note:
                                ui.label(note).classes(f"text-xs {color}")
                            with ui.row().classes("gap-3 text-xs text-gray-400 items-center"):
                                ui.label(COND.get(r["condition_id"], "品相未标"))
                                # 非东京的也显示出来 —— 不然「这件为什么没有东京标」
                                # 你分不清是「不在东京」还是「还没拉详情」
                                if r["ship_from"]:
                                    ui.label(f"发货 {r['ship_from']}")
                                # 【必须用默认参数绑死 rid/sid】这两个是循环变量，
                                # 直接在 lambda 里引用 rule/r 的话，等你点下去时它们
                                # 早就指向循环的最后一件商品了 —— 每个按钮都会拉黑同一个人。
                                # 卖家ID为空时不给按钮：ヤフオク 有一部分商品不给卖家ID，
                                # 没有可拉黑的对象，画个点不动的按钮只会让人以为坏了。
                                if r["seller_id"]:
                                    ui.button(
                                        "拉黑卖家",
                                        on_click=lambda _, rid=rule["id"], sid=r["seller_id"]:
                                            blacklist_seller(rid, sid),
                                    ).props("flat dense no-caps size=sm color=negative") \
                                     .classes("text-xs px-1").tooltip(
                                        f"卖家 {r['seller_id']}\n"
                                        "拉黑后这条规则下他的全部商品立刻判为不合适。"
                                        "想反悔就去规则页把 ID 从 exclude_sellers 里删掉")
                                if r["price"] < r["first_price"]:
                                    ui.label(f"已降 {yen(r['first_price'] - r['price'])}"
                                             f"（首见 {yen(r['first_price'])}）").classes("text-red-400")
                                ui.label(f"上架 {r['listed_at']:%m-%d %H:%M}" if r["listed_at"] else "")
                        # shrink-0：价格列宽度固定，不参与压缩
                        # whitespace-nowrap：¥1,188,800 这种数字本身也绝不折行
                        with ui.column().classes("gap-0 items-end shrink-0 whitespace-nowrap"):
                            # 来源放在价格正上方：这两个信息是一起看的 ——
                            # 同一个价格在哪个平台，直接决定你怎么去买
                            ui.badge(source_name(r["source"]), color="blue-grey").classes("mb-1")
                            ui.label(yen(r["price"])).classes("text-lg font-bold")
                            if r["deal_pct"]:
                                ui.label(f"市价的 {r['deal_pct']}%").classes(
                                    "text-xs " + ("text-green-400" if r["is_deal"] else "text-gray-400"))


def _can_blacklist(row: dict, rule: dict) -> bool:
    """这一行能不能拉黑：有卖家ID、且还没在该规则的黑名单里。"""
    sid = (row.get("seller_id") or "").strip().lower()
    return bool(sid) and sid not in ids(rule.get("exclude_sellers"))


def _act_label(row: dict, rule: dict) -> str:
    if not (row.get("seller_id") or "").strip():
        return "—"
    return "拉黑" if _can_blacklist(row, rule) else "已拉黑"


def save_deal_price(rule_id: int, value) -> None:
    """设手动捡漏价，并【立刻】重算一遍，不用等下一轮。

    立刻重算是因为这是个手动动作：你就是想马上看到哪些变成捡漏了。
    mark_deals 是纯 CPU、不发请求，同步跑一次只是几十行 UPDATE。
    """
    try:
        v = int(float(value or 0))
    except (TypeError, ValueError):
        notify("捡漏价要填数字", type="warning")
        return
    if v < 0:
        notify("捡漏价不能是负数", type="warning")
        return
    rule = store.get_rule(rule_id)
    if not rule:
        notify("这条规则不在了（可能刚被删掉）", type="warning")
        return
    store.update_rule(rule_id, {"deal_price": v})
    poller.mark_deals(store.get_rule(rule_id))
    if v:
        notify(f"「{rule['name']}」捡漏价设为 {yen(v)}，已重算")
    else:
        notify(f"「{rule['name']}」已清空手动价，回到按成交中位数的百分比判")
    hits_view.refresh()
    sold_view.refresh()


# ------------------------------------------------------------------ 成交页

@ui.refreshable
def sold_view() -> None:
    """成交页：市场实际用什么价把什么货清掉了。

    【为什么要单独一栏】命中页回答「现在有什么能买」，靠的是和市价中位数比。
    但那个中位数是个单一数字，看不出它底下的分布：是十几件挤在一个价位，
    还是从 40 万到 90 万拉了一条长线。定价前要看的是后者。

    两类数据粒度不同，都得摆出来：
      跟到成交的  item.status='sold_out'，有标题缩略图，能看清「什么货、什么价、
                  降了多少才卖掉」。只有走完「我们一直在跟 → 它从搜索结果消失 →
                  对账确认售出」这条链路的才会进来，所以量少但信息最全。
      成交价样本  sold_sample，只有价格和时间（成交轮按标题级规则扫来的，没拉详情），
                  量大，是中位数的实际依据。
    """
    rules = store.get_rules()
    if not rules:
        ui.label("还没有规则，去「规则」页新建一条。").classes("text-gray-400 p-4")
        return

    for rule in rules:
        rid = rule["id"]
        st = store.get_state(rid)
        med = st["median_price"]
        tracked = store.query(
            "SELECT * FROM item WHERE rule_id = %s AND status = 'sold_out' "
            "ORDER BY sold_at DESC", (rid,))
        samples = store.query(
            "SELECT source, price, sold_at, sample_kind FROM sold_sample "
            "WHERE rule_id = %s ORDER BY sold_at DESC LIMIT 80", (rid,))

        # 折叠摘要：折起来后这一行就是全部，所以中位数和价格区间都要在里面
        cap = [f"近{rule['median_window_days']}天"]
        if samples:
            ps = sorted(x["price"] for x in samples)
            cap.append(f"成交 {len(samples)} 件　{yen(ps[0])} 〜 {yen(ps[-1])}")
        if med:
            cap.append(f"中位 {yen(med)}")
            if rule["deal_ratio"]:
                cap.append(f"捡漏线 {yen(med * rule['deal_ratio'] // 100)}")
        else:
            cap.append(f"样本不足 {rule['median_min_samples']} 件，暂不出中位数")
        head = f"{rule['name']}　成交样本 {len(samples)} 件" + (
            f"　🔗 跟到成交 {len(tracked)} 件" if tracked else "")

        with ui.expansion(head, caption="　·　".join(cap), value=True) \
                .classes("w-full mb-3").props("header-class=text-base"):
            if not samples and not tracked:
                ui.label("还没有成交数据。成交轮每 "
                         f"{rule['sold_scan_hours']} 小时跑一次，跑过之后这里才有东西。"
                         ).classes("text-gray-400 text-sm")
                continue

            if tracked:
                ui.label("我们一路跟到成交的（拉过详情、过了完整规则，信息最全）") \
                    .classes("text-sm text-gray-300 mt-1")
                with ui.element("div").classes(
                        "grid grid-cols-1 xl:grid-cols-2 gap-x-6 w-full"):
                    for r in tracked:
                        _sold_row(r, med)




def _sold_row(r: dict, med: int | None) -> None:
    """一件跟到成交的商品。布局和命中页同一套，理由见那边的注释。"""
    with ui.row().classes("items-start w-full gap-3 border-t py-2 flex-nowrap"):
        if r["thumb_url"]:
            ui.image(r["thumb_url"]).classes("w-24 h-24 object-cover rounded shrink-0")
        with ui.column().classes("gap-0 grow min-w-0 leading-snug"):
            with ui.row().classes("items-center gap-2 flex-wrap mb-1"):
                ui.badge("已成交", color="grey")
                if r["ship_from"] == TOKYO:
                    ui.badge(TOKYO, color="teal")
                if r["is_deal"]:
                    # 卖掉的捡漏货 = 你错过的那些。摆出来是为了让你知道
                    # 这个价位真的会被人买走，下次别犹豫。
                    ui.badge("曾是捡漏", color="green").tooltip(
                        "它在售时低于捡漏线 —— 也就是这个价位确实有人接")
            ui.link(r["name"], item_url(r["source"], r["item_id"]),
                    new_tab=True).classes("font-medium break-words")
            with ui.row().classes("gap-3 text-xs text-gray-400 items-center"):
                ui.label(f"{r['sold_at']:%m-%d %H:%M} 成交" if r["sold_at"] else "成交时间未知")
                if r["ship_from"]:
                    ui.label(f"发货 {r['ship_from']}")
                # 【降了多少才卖掉】这是定价最直接的参考：挂多少没人要、降到多少成交
                if r["price"] < r["first_price"]:
                    ui.label(f"从 {yen(r['first_price'])} 降了 "
                             f"{yen(r['first_price'] - r['price'])} 才卖掉").classes("text-red-400")
        with ui.column().classes("gap-0 items-end shrink-0 whitespace-nowrap"):
            ui.badge(source_name(r["source"]), color="blue-grey").classes("mb-1")
            ui.label(yen(r["price"])).classes("text-lg font-bold")
            if med:
                ui.label(f"市价的 {r['price'] * 100 // med}%").classes("text-xs text-gray-400")




# ------------------------------------------------------------------ 全部页

@ui.refreshable
def all_view(rule_id: int | None, reason: str) -> None:
    where, args = ["1=1"], []
    if rule_id:
        where.append("rule_id = %s"); args.append(rule_id)
    if reason != "全部":
        key = next(k for k, v in REASON_LABEL.items() if v == reason)
        where.append("reject_reason = %s"); args.append(key)

    rows = store.query(
        # 【必须覆盖 explain() 读的每一个字段】少一个，「具体原因」那一列就会
        # 静默退化成兜底文案（seller_id 漏掉时每行都显示「卖家 ? 在黑名单里」），
        # 而这一列存在的全部意义就是说清楚是哪个条件、哪个值判掉的。
        f"SELECT source, item_id, rule_id, name, price, matched, reject_reason, status, "
        f"condition_id, seller_id, desc_warn, desc_checked, bid_count, buy_now_price, "
        f"end_time, first_seen_at FROM item "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY first_seen_at DESC LIMIT 300", args)
    # 拿完整规则（不只是名字）：原因列要用规则里的词表和阈值把「为什么」算出来
    rules = {r["id"]: r for r in store.get_rules()}

    ui.label(f"{len(rows)} 件（最多显示 300 件，按发现时间倒序）").classes("text-sm text-gray-400")
    tbl = ui.table(
        columns=[
            {"name": "rule", "label": "规则", "field": "rule", "align": "left"},
            {"name": "src", "label": "来源", "field": "src", "align": "left", "sortable": True},
            # 【放原始数字，不放格式化字符串】Quasar 的默认排序先判 isNumber，
            # 拿到 "¥1,188,800" 这种字符串就退化成字典序 —— 升序会排成
            # ¥1,188,800 < ¥12,000 < ¥2,000，最贵的跑到最前面。
            # 而这一页正是用来「按价格找是不是上限卡太死」的，顺序假了就白看。
            # 显示交给下面的 body-cell-price 插槽。
            {"name": "price", "label": "价格", "field": "price", "align": "right", "sortable": True},
            {"name": "reason", "label": "判定", "field": "reason", "align": "left", "sortable": True},
            {"name": "detail", "label": "具体原因", "field": "detail", "align": "left"},
            {"name": "status", "label": "状态", "field": "status", "align": "left"},
            {"name": "name", "label": "标题（点击打开商品页）", "field": "name", "align": "left"},
            {"name": "act", "label": "卖家", "field": "act", "align": "left"},
        ],
        rows=[{
            "id": f"{r['source']}-{r['item_id']}-{r['rule_id']}",
            "rule": (rules.get(r["rule_id"]) or {}).get("name", "?"),
            "src": source_name(r["source"]),
            "price": r["price"],
            "reason": REASON_LABEL.get(r["reject_reason"], r["reject_reason"]),
            "detail": explain(rules.get(r["rule_id"]) or {}, r),
            "status": {"on_sale": "在售", "sold_out": "已售出",
                       "trading": "交易中", "gone": "已下架"}.get(r["status"], r["status"]),
            "name": r["name"],
            # 下面三个不作为列显示，只放在行数据里给模板和事件回调取用。
            # rule_id 必须逐行带：这一页可以同时显示多条规则的商品（筛选选「全部规则」时），
            # 拉黑只能落在该行自己那条规则上。
            "link": item_url(r["source"], r["item_id"]),
            "seller": r["seller_id"],
            "rule_id": r["rule_id"],
            # 【按钮的文案和可点性在这里算好】插槽模板每行只实例化一份同样的元素，
            # Python 侧没法逐行控制，所以条件得预先算成行数据、模板里只做绑定。
            "act": _act_label(r, rules.get(r["rule_id"]) or {}),
            "can_bl": _can_blacklist(r, rules.get(r["rule_id"]) or {}),
        } for r in rows],
        row_key="id", pagination=50,
    )
    # 【两个 add_slot 必须分开写】Element.add_slot() 返回的是 Slot 不是 Element，
    # 链式接第二个 add_slot 会 AttributeError: 'Slot' object has no attribute 'add_slot'
    tbl.add_slot("body-cell-price", r'''
        <q-td :props="props" class="text-right">
          {{ props.value == null ? "—" : "¥" + Number(props.value).toLocaleString() }}
        </q-td>
    ''')
    tbl.add_slot("body-cell-name", r'''
        <q-td :props="props">
          <a :href="props.row.link" target="_blank" rel="noopener noreferrer"
             class="text-blue-400 hover:underline">{{ props.value }}</a>
        </q-td>
    ''')
    # 【这一格用 NiceGUI 元素，不用裸 HTML】上面两个插槽是纯展示，裸模板就够了；
    # 这一格要回调到 Python，走 table.cell + 元素自己的 on() 是官方支持的路径，
    # 不用去赌 `$parent.$emit` 在 scoped slot 里指向哪个组件。
    # 文案三态：没有卖家ID → 「—」（ヤフオク 有一部分商品不给，メルカリShops
    # 的卖家是店铺不是用户）；已经在黑名单里 → 「已拉黑」；其余才可点。
    with tbl.add_slot("body-cell-act"):
        with tbl.cell("act"):
            ui.button().props(
                'flat dense no-caps size=sm color=negative '
                ':label="props.row.act" :disable="!props.row.can_bl"'
            ).on(
                "click",
                # 【rule_id 必须逐行取】这一页可以同时列出多条规则的商品，
                # 拉黑只能落在该行自己那条规则上。
                js_handler="() => emit(props.row.rule_id, props.row.seller)",
                handler=lambda e: blacklist_seller(e.args[0], e.args[1]),
            )


# ------------------------------------------------------------------ 规则页

def rule_dialog(rule: dict | None, host=None) -> None:
    """新建/编辑规则。rule=None 表示新建。

    【host 必须是页面级容器，不能省】对话框默认会建在「触发它的那个按钮」所在的
    插槽里，也就是 rules_view 的刷新容器内部。而 refreshable.refresh() 的第一步是
    container.clear()，会把所有后代删掉 —— 于是：你点「立即跑一轮」（要跑几分钟），
    等待期间去编辑另一条规则的排除词表，那一轮跑完时 run_now 末尾无条件
    rules_view.refresh()，对话框连同你刚敲的一大段内容凭空消失，界面上只弹一个
    绿色的「完成」，没有任何提示说你的编辑被吃了。
    """
    data = dict(rule) if rule else {
        "name": "", "enabled": 1, "keyword": "", "sources": "",
        "include_all": "", "include_any": "",
        "exclude_any": "", "warn_desc": "", "exclude_sellers": "",
        "price_min": 0, "price_max": 0,
        "condition_ids": "", "allow_shops": 0, "check_desc": 1,
        "deal_price": 0, "deal_ratio": 85, "quick_min": 7, "note": "",
    }
    with (host or ui.context.client.content):
        dlg = ui.dialog()
    with dlg, ui.card().classes("w-[760px] max-w-full"):
        ui.label("编辑规则" if rule else "新建规则").classes("text-lg font-bold")
        with ui.column().classes("w-full gap-2"):
            for f in ("name", "keyword", "include_all", "include_any"):
                ui.input(f, value=data[f]).classes("w-full").props("dense outlined") \
                    .bind_value(data, f).tooltip(FIELD_HELP.get(f, ""))
                ui.label(FIELD_HELP.get(f, "")).classes("text-xs text-gray-400 -mt-2")
            for f in ("exclude_any", "warn_desc", "exclude_sellers"):
                ui.textarea(f, value=data[f]).classes("w-full").props("dense outlined rows=3") \
                    .bind_value(data, f)
                ui.label(FIELD_HELP.get(f, "")).classes("text-xs text-gray-400 -mt-2")
            with ui.row().classes("w-full gap-3"):
                ui.number("价格下限 ¥", value=data["price_min"], format="%d") \
                    .props("dense outlined").bind_value(data, "price_min")
                ui.number("价格上限 ¥（0=不限）", value=data["price_max"], format="%d") \
                    .props("dense outlined").bind_value(data, "price_max")
                ui.number("手动捡漏价 ¥（0=不用）", value=data["deal_price"], format="%d") \
                    .props("dense outlined").bind_value(data, "deal_price") \
                    .tooltip("低于它就算捡漏。【填了就完全盖过右边的百分比】"
                             "它的意义是不依赖成交样本 —— 规则刚建、或某个型号成交太少"
                             "算不出中位数时，百分比那套整个不工作，而你心里是有价的")
                ui.number("捡漏线 %", value=data["deal_ratio"], format="%d") \
                    .props("dense outlined").bind_value(data, "deal_ratio") \
                    .tooltip("低于「成交中位数 × 此值%」时标捡漏。0=关闭。"
                             "左边填了手动价的话这一项不生效")
                ui.number("扫描间隔 分", value=data["quick_min"], format="%d", min=1) \
                    .props("dense outlined").bind_value(data, "quick_min") \
                    .tooltip("至少 1 分钟。这个值直接决定对外发请求的频率")
            ui.input("品相白名单", value=data["condition_ids"]).classes("w-full") \
                .props("dense outlined").bind_value(data, "condition_ids") \
                .tooltip(FIELD_HELP["condition_ids"])
            ui.label(FIELD_HELP["condition_ids"]).classes("text-xs text-gray-400 -mt-2")
            ui.input("备注", value=data["note"]).classes("w-full").props("dense outlined") \
                .bind_value(data, "note")
            # 数据源多选：全不选＝全部源（和 sources 列留空等价）
            all_keys = list(sources.all_sources())
            picked = {k: (k in (data["sources"] or "").split(",")) for k in all_keys}
            with ui.row().classes("items-center gap-4"):
                ui.label("数据源").classes("text-sm")
                for k in all_keys:
                    ui.switch(sources.get(k).name, value=picked[k]).bind_value(picked, k)
            ui.label("全不选 = 全部源。各源的限速状态（退避档位、上次请求时间）互相独立，"
                     "但轮询只有一条线程 —— 某个源退避期间，别的源那一轮会被顺延"
                     ).classes("text-xs text-gray-400 -mt-2")

            with ui.row().classes("gap-6"):
                ui.switch("启用", value=bool(data["enabled"])).bind_value(data, "enabled")
                ui.switch("收 Shops 商家品", value=bool(data["allow_shops"])) \
                    .bind_value(data, "allow_shops")
                ui.switch("拉详情查描述", value=bool(data["check_desc"])) \
                    .bind_value(data, "check_desc")

        def save() -> None:
            chosen = [k for k, v in picked.items() if v]
            data["sources"] = "" if len(chosen) == len(all_keys) else ",".join(chosen)
            if not data["name"] or not data["keyword"]:
                notify("规则名和搜索词不能为空", type="warning")
                return
            # 【quick_min 必须 ≥1，不能走 clean() 的 None→0】
            # ui.number 被清空时 value 是 None，粘贴「7 分钟」这种带非数字的文本也是 None。
            # 一旦落成 0，poller 的 _due(last, 0) 恒为真 —— 这条规则的每个源都会在
            # 每 30 秒一次的 tick 上被整轮重扫，几小时就撞满 daily_request_limit，
            # 然后【所有规则所有源】一起停抓到次日。
            # 其它数字框的 0 是合法语义（价格上下限 0=不限、捡漏线 0=关闭），所以只拦这一个。
            try:
                qm = int(data.get("quick_min") or 0)
            except (TypeError, ValueError):
                qm = 0
            if qm < 1:
                notify("扫描间隔至少 1 分钟（填 0 会让这条规则每 30 秒全源重扫，"
                       "几小时就会烧穿当天的请求配额）", type="warning")
                return
            data["quick_min"] = qm
            # 这三个清空＝按各自的「不限 / 关闭」语义走，是合法的
            for k in ("price_min", "price_max", "deal_ratio", "deal_price"):
                if data.get(k) is None:
                    data[k] = 0
            # ui.number 被清空时 value 是 None，而这些列都是 NOT NULL；
            # ui.switch 给的是 bool，ui.number 给的是 float —— 统一收成 int。
            def clean(v):
                if isinstance(v, bool):
                    return int(v)
                if isinstance(v, float) or v is None:
                    return int(v or 0)
                return v

            payload = {k: clean(v) for k, v in data.items() if k in store.RULE_FIELDS}
            if rule:
                # 【只写真正改过的字段，不要整行覆盖】data 是打开对话框那一刻的快照，
                # 而这个对话框是刻意建在 dialog_host 里的（要能撑过几分钟的「立即跑一轮」），
                # 开着的时候你完全可以切到命中页点「拉黑卖家」。整行写回会拿旧快照
                # 把 exclude_sellers 冲回空串 —— 两次操作都提示成功，而拉黑没了，
                # 下一轮那些商品又回到命中列表，人只会觉得「黑名单不生效」。
                before = {k: clean(v) for k, v in rule.items() if k in store.RULE_FIELDS}
                payload = {k: v for k, v in payload.items() if v != before.get(k)}
                if not payload:
                    close()
                    notify("没有任何改动")
                    return
                store.update_rule(rule["id"], payload)
            else:
                store.insert_rule(payload)
            close()
            rules_view.refresh()
            hits_view.refresh()
            notify("已保存，下一轮生效")

        def close() -> None:
            # 关了就删掉：每点一次「编辑」都会新建一个 dialog 元素，
            # 只 close 不 delete 的话它们会一直挂在页面上越积越多。
            dlg.close()
            dlg.delete()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=close).props("flat")
            ui.button("保存", on_click=save).props("color=primary")
    dlg.open()


def confirm_delete(rule: dict, host=None) -> None:
    with (host or ui.context.client.content):
        dlg = ui.dialog()
    with dlg, ui.card():
        ui.label(f"删除规则「{rule['name']}」？")
        ui.label("它抓到的商品和成交样本会一并删除，不可恢复。").classes("text-sm text-red-400")

        def close() -> None:
            dlg.close()
            dlg.delete()

        def do() -> None:
            store.delete_rule(rule["id"])
            close()
            rules_view.refresh()
            hits_view.refresh()
            notify("已删除")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=close).props("flat")
            ui.button("删除", on_click=do).props("color=negative")
    dlg.open()


@ui.refreshable
def rules_view(host=None) -> None:
    ui.button("新建规则", on_click=lambda: rule_dialog(None, host)).props("color=primary")
    for rule in store.get_rules():
        st = store.get_state(rule["id"])
        # 【命中＝当前在售的命中】和「命中」页显示的是同一批。
        # 用 SUM(matched) 会把已售出/已结束的历史命中也算进来，同一个词两个意思。
        stats = store.one(
            "SELECT COUNT(*) total, SUM(matched = 1 AND status = 'on_sale') hit "
            "FROM item WHERE rule_id = %s", (rule["id"],))
        with ui.card().classes("w-full my-2"):
            with ui.row().classes("items-center w-full gap-3"):
                ui.label(rule["name"]).classes("text-base font-bold")
                ui.badge("启用" if rule["enabled"] else "停用",
                         color="green" if rule["enabled"] else "grey")
                ui.label(f'搜「{rule["keyword"]}」').classes("text-sm")
                ui.label(f"{yen(rule['price_min'])}〜{yen(rule['price_max'])}").classes("text-sm")
                ui.space()
                ui.label(f"入库 {stats['total'] or 0} · 在售命中 {int(stats['hit'] or 0)}"
                         ).classes("text-sm text-gray-400")
            ui.label(f"市价中位 {yen(st['median_price'])}"
                     f"（跨源 {st['sample_count']} 件成交）").classes("text-xs text-blue-400")

            # 每个数据源单独一行：各源独立计时、独立限速，状态也分开看
            for src in sources.for_rule(rule):
                ss = store.get_source_state(rule["id"], src.key)
                n = store.one("SELECT COUNT(*) t, SUM(matched = 1 AND status = 'on_sale') h "
                              "FROM item WHERE rule_id = %s AND source = %s",
                              (rule["id"], src.key))
                with ui.row().classes("gap-3 text-xs text-gray-400 items-center"):
                    ui.badge(src.name, color="blue-grey")
                    ui.label(f"上次扫描 {ss['last_scan_at']:%m-%d %H:%M}"
                             if ss["last_scan_at"] else "还没扫过")
                    ui.label(f"在售 {ss['last_total']}")
                    ui.label(f"入库 {n['t'] or 0} · 在售命中 {int(n['h'] or 0)}")
                    if ss["truncated"]:
                        ui.label("⚠ 上次没扫全，售出对账已跳过").classes("text-orange-400")
                    if ss["last_error"]:
                        ui.label(f"⚠ {ss['last_error'][:60]}").classes("text-red-400")
            if rule["note"]:
                ui.label(rule["note"]).classes("text-xs text-gray-400")
            with ui.row().classes("gap-2"):
                ui.button("编辑", on_click=lambda r=rule: rule_dialog(r, host)).props("flat dense")
                ui.button("立即跑一轮", on_click=lambda r=rule: run_now(r)).props("flat dense")
                ui.button("删除", on_click=lambda r=rule: confirm_delete(r, host)
                          ).props("flat dense color=negative")


# 【进程级的重入闸】面板是多标签页的，而抓取是服务端动作 ——
# 两个标签页各点一次，就是两轮并发打同一批源。各源的 _lock 会把请求串起来，
# 所以不会绕过节流，但配额会白烧一倍、日志也会交错到看不懂。
_fetching = {"busy": False}


async def fetch_all() -> None:
    """一键抓取：所有启用的规则立刻各跑一轮（常驻轮询照常继续，两边共用同一套限速）。"""
    if _fetching["busy"]:
        notify("已经在抓了，等这一轮跑完再点", type="warning")
        return
    rules = store.get_rules(enabled_only=True)
    if not rules:
        notify("没有启用的规则", type="warning")
        return
    _fetching["busy"] = True
    try:
        st = store.get_settings()
        notify(f"开始抓 {len(rules)} 条规则 × {len(sources.all_sources())} 个源，"
               f"每个请求间隔 {st['req_delay_min']:g}〜{st['req_delay_max']:g} 秒，要几分钟…")
        new = matched = 0
        for r in rules:
            try:
                # 【每条规则单独 try】一条失败不该让后面几条一起放弃
                stat = await run.io_bound(poller.run_once, store.get_rule(r["id"]))
                new += stat["new"]
                matched += stat["matched"]
            except Exception as e:          # noqa: BLE001 - 手动抓取失败只弹提示
                notify(f"「{r['name']}」失败：{e}", type="negative")
        notify(f"抓完：新增 {new} 件，当前命中 {matched} 件", type="positive")
    finally:
        # 【必须在 finally】中途抛异常而不解锁的话，这个按钮就永久点不动了，
        # 而且没有任何办法恢复，只能重启进程。
        _fetching["busy"] = False
    hits_view.refresh()
    rules_view.refresh()
    all_view.refresh()


async def run_now(rule: dict) -> None:
    """面板上的手动试跑。走 io_bound 扔到线程里 —— 一轮要遍历所有源、发好几个请求、
    每个之间还要等 3〜8 秒，直接在事件循环里跑会把整个面板卡死。"""
    srcs = "、".join(x.name for x in sources.for_rule(rule))
    st = store.get_settings()          # 别硬编码，这两个值在设置页可改
    notify(f"开始跑「{rule['name']}」（{srcs}），"
           f"每个请求间隔 {st['req_delay_min']:g}〜{st['req_delay_max']:g} 秒，请稍候…")
    try:
        stat = await run.io_bound(poller.run_once, store.get_rule(rule["id"]))
        notify(f"完成：在售{stat['total']}件 新增{stat['new']} "
               f"降价{stat['price_down']} 命中{stat['matched']}", type="positive")
    except Exception as e:  # noqa: BLE001 - 手动试跑失败只该弹个提示，不该影响面板
        notify(f"失败：{e}", type="negative")
    rules_view.refresh()
    hits_view.refresh()


# ------------------------------------------------------------------ 设置页

@ui.refreshable
def settings_view() -> None:
    ui.label("这些是全局设置，对所有规则生效。改完保存，10 秒内自动生效，不用重启。").classes(
        "text-sm text-gray-400")

    fields: dict = {}
    for it in store.all_settings():
        with ui.card().classes("w-full my-1 py-2"):
            # 【字符串项必须用 ui.input】拿 ui.number 装 URL 会直接显示成空白，
            # 而且保存时 value 是 None —— 看起来像"填了没保存上"。
            if it["type"] == "str":
                comp = ui.input(it["k"], value=it["v"]).classes("w-full").props("dense outlined")
            else:
                comp = ui.number(it["k"], value=it["v"],
                                 format="%d" if it["type"] == "int" else "%.1f") \
                    .classes("w-full").props("dense outlined")
            fields[it["k"]] = (comp, it)
            # 说明里有换行（推送那几项列了各家的地址格式），预留换行才看得清
            ui.label(it["note"]).classes("text-xs text-gray-400 whitespace-pre-line")
            ui.label(f"默认值：{it['default']!r}" if it["type"] == "str"
                     else f"默认值：{it['default']}").classes("text-xs text-gray-400")

    # 这几项填 0 不是「关闭」而是各种翻车：
    #   sold_scan_hours=0  成交轮每 30 秒重跑一次
    #   max_pages=0        一件都扫不到，truncated 却判 False，于是售出对账
    #                      把全库在售当成失踪逐个花详情核实
    #   detail_budget=0    永远不核实、不读描述
    #   median_window_days=0  prune 会把整张 sold_sample 删空，tracked 样本再也抓不回来
    #   fresh_hours=0      两个「新」徽标永远不出现
    MUST_BE_POSITIVE = ("sold_scan_hours", "max_pages", "detail_budget",
                        "median_window_days", "fresh_hours", "daily_request_limit",
                        "missing_grace_min", "req_delay_min", "req_delay_max")

    def save() -> None:
        bad, bad_zero = [], []
        for k, (comp, it) in fields.items():
            val = comp.value
            # 【字符串项的空值是合法的】notify_url 留空就是"关掉推送"，
            # 走下面那条 bad 分支会变成"跳过不写"，于是根本关不掉。
            if it["type"] == "str":
                store.save_setting(k, val or "")
                continue
            if val is not None and k in MUST_BE_POSITIVE:
                try:
                    if float(val) <= 0:
                        bad_zero.append(k)
                        continue
                except (TypeError, ValueError):
                    pass
            if val is None:
                bad.append(k)                  # 空值会让该项回落默认，多半是误删，拦一下
                continue
            store.save_setting(k, val)
        settings_view.refresh()
        if bad_zero:
            notify(f"{'、'.join(bad_zero)} 不能填 0 或负数（这几项的 0 不是「关闭」，"
                   f"而是会让轮询退化或把成交样本删空），已跳过未保存", type="negative")
        if bad:
            # 【原来写的是「会按默认值走」，和事实正好相反】代码是 continue 跳过不写，
            # 库里的旧值继续生效。照着错提示操作的人会以为自己成功恢复了默认值。
            notify(f"已保存。{'、'.join(bad)} 留空了——【没有改动】，库里原来的值继续生效；"
                   f"要改请填具体数字", type="warning")
        else:
            notify("已保存，10 秒内生效")

    ui.button("保存全部", on_click=save).props("color=primary").classes("mt-2")


# ------------------------------------------------------------------ 组装

def create() -> None:
    @ui.page("/")
    def index() -> None:
        ui.dark_mode(True)
        # 主色 blue-400 / 负色 red-400，用 oklch —— P3 屏上不走 sRGB 夹紧
        ui.colors(primary="oklch(70.7% 0.165 254.624)", negative="oklch(70.4% 0.191 22.216)")
        ui.add_head_html(DARK_CSS)
        # 顶栏默认会被染成主色（亮蓝），暗色下太跳；压成近黑并用淡描边收边
        with ui.header().classes("items-center justify-between px-4 py-2").style(
                "background:#15171c;border-bottom:1px solid rgba(255,255,255,.08);box-shadow:none"):
            ui.label("Resale Watcher").classes("text-lg font-bold")
            status = ui.label().classes("text-sm")

        def tick() -> None:
            # 【先看采集还活着没】轮询线程崩掉后进程照常在跑、面板照常打开、
            # 旧数据照常显示，只有「上次扫描」的时间戳冻住 —— 不主动报的话
            # 你可能几天都以为它在监控。
            beat = poller.heartbeat()
            dead = beat is None or (config.now() - beat) > timedelta(minutes=3)
            try:
                d = store.today_stat()
                per = "  ".join(f"{source_name(x['source'])} {x['requests']}"
                                for x in store.today_by_source())
                text = (f"今日请求 {d['requests']}/{store.get_settings()['daily_request_limit']}"
                        + (f"（{per}）" if per else "")
                        + f"　失败 {d['errors']}　{config.now():%H:%M:%S}")
            except Exception as e:          # noqa: BLE001 - 数据库抽风不该让状态栏整个消失
                text = f"⚠ 读数据库失败：{str(e)[:60]}"
            if dead:
                gap = f"（心跳停在 {beat:%H:%M:%S}）" if beat else "（从未启动）"
                status.text = f"⚠ 轮询已停止{gap}　" + text
                status.classes(replace="text-sm text-red-400 font-bold")
            else:
                status.text = text
                status.classes(replace="text-sm")

        tick()
        ui.timer(10.0, tick)

        # 对话框的家：建在所有 refreshable 容器之外，refresh() 清不到它
        dialog_host = ui.element()

        with ui.tabs().classes("w-full") as tabs:
            t_hit = ui.tab("命中")
            t_sold = ui.tab("成交")
            t_all = ui.tab("全部")
            t_rule = ui.tab("规则")
            t_set = ui.tab("设置")
        with ui.tab_panels(tabs, value=t_hit).classes("w-full"):
            with ui.tab_panel(t_hit):
                with ui.row().classes("items-center gap-2"):
                    ui.button("一键抓取", on_click=fetch_all).props("color=primary dense no-caps") \
                        .tooltip("所有启用的规则立刻各跑一轮。常驻轮询照常继续，"
                                 "两边共用同一套限速，不会因此发得更快")
                    ui.button("刷新", on_click=hits_view.refresh).props("flat dense no-caps") \
                        .tooltip("只重画页面，不发请求")
                hits_view()
            with ui.tab_panel(t_sold):
                with ui.row().classes("items-center gap-2"):
                    ui.button("刷新", on_click=sold_view.refresh).props("flat dense no-caps")
                    ui.label("市场实际用什么价清掉了什么货 —— 定价前先看这一页的分布，"
                             "别只看中位数那一个数字").classes("text-xs text-gray-400")
                sold_view()
            with ui.tab_panel(t_all):
                rules = store.get_rules()
                opts = {None: "全部规则", **{r["id"]: r["name"] for r in rules}}
                with ui.row().classes("items-center gap-3"):
                    sel_rule = ui.select(opts, value=None).props("dense outlined")
                    sel_reason = ui.select(["全部"] + list(REASON_LABEL.values()),
                                           value="全部").props("dense outlined")
                    ui.button("刷新", on_click=lambda: all_view.refresh(
                        sel_rule.value, sel_reason.value)).props("flat dense")
                sel_rule.on_value_change(lambda: all_view.refresh(sel_rule.value, sel_reason.value))
                sel_reason.on_value_change(lambda: all_view.refresh(sel_rule.value, sel_reason.value))
                all_view(None, "全部")
            with ui.tab_panel(t_rule):
                rules_view(dialog_host)
            with ui.tab_panel(t_set):
                settings_view()
