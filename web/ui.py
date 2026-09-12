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
    # 【这半条和上面是同一个决定】Quasar 的 .q-badge{color:#fff} 是写死的白字，
    # 而上面这批背景亮度在 70%~83% —— amber 上白字只有 1.7:1、green 2.2:1，全线看不清。
    # 亮底一律改配深色前景。
    ".q-badge.bg-green,.q-badge.bg-orange,.q-badge.bg-amber,.q-badge.bg-grey"
    "{color:#18181b!important}"
    "}"
    "</style>"
)

COND = {1: "新品未使用", 2: "未使用に近い", 3: "傷汚れなし",
        4: "やや傷汚れ", 5: "傷や汚れあり", 6: "状態が悪い"}

REASON_LABEL = {
    "": "合适", "price_over": "超预算", "price_under": "低于下限",
    "excluded_title": "标题排除词",
    # no_keyword 平时不会入库（第一层判定直接丢弃），但 poller.revalidate 会用当前规则
    # 重判【已在库】的商品 —— 你把必含词改严之后，老商品就会被写成这个原因留在库里
    "no_keyword": "关键词不匹配",
    "condition": "品相不符", "shop_item": "Shops商家品",
    # 历史遗留：描述判定改成只打标签之后就不再产生这个原因了，留着只为看懂老数据
    "excluded_desc": "描述排除词(已废弃)",
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
            if rule["deal_ratio"]:
                summary.append(f"低于 {yen(med * rule['deal_ratio'] // 100)} 算捡漏")
        else:
            summary.append(f"成交样本不足{rule['median_min_samples']}件，暂无市价参考")
        deals = sum(1 for r in rows if r["is_deal"])
        head = f"{rule['name']}　{len(rows)} 件" + (f"　🟢 {deals} 件捡漏" if deals else "")

        # value=True＝默认展开。折叠状态只活在当前页面里，刷新后回到展开 ——
        # 这正是想要的：命中列表是每次打开都要从头扫一遍的东西。
        with ui.expansion(head, caption=" · ".join(summary), value=True) \
                .classes("w-full mb-3").props("header-class=text-base"):
            if not rows:
                ui.label("当前没有符合条件的在售商品。").classes("text-gray-400 text-sm")
                continue

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
                        if r["is_deal"] or fresh or r["desc_warn"]:
                            with ui.row().classes("items-center gap-2 flex-wrap mb-1"):
                                if r["is_deal"]:
                                    ui.badge("捡漏", color="green")
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
                        with ui.row().classes("gap-3 text-xs text-gray-400"):
                            ui.label(COND.get(r["condition_id"], "品相未标"))
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
        f"SELECT source, item_id, rule_id, name, price, matched, reject_reason, status, "
        f"condition_id, desc_warn, desc_checked, bid_count, buy_now_price, end_time, "
        f"first_seen_at FROM item "
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
            # link 不作为一列显示，只放在行数据里给下面的模板取用
            "link": item_url(r["source"], r["item_id"]),
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


# ------------------------------------------------------------------ 规则页

def rule_dialog(rule: dict | None) -> None:
    """新建/编辑规则。rule=None 表示新建。"""
    data = dict(rule) if rule else {
        "name": "", "enabled": 1, "keyword": "", "sources": "",
        "include_all": "", "include_any": "",
        "exclude_any": "", "warn_desc": "", "price_min": 0, "price_max": 0,
        "condition_ids": "", "allow_shops": 0, "check_desc": 1,
        "deal_ratio": 85, "quick_min": 7, "note": "",
    }
    with ui.dialog() as dlg, ui.card().classes("w-[760px] max-w-full"):
        ui.label("编辑规则" if rule else "新建规则").classes("text-lg font-bold")
        with ui.column().classes("w-full gap-2"):
            for f in ("name", "keyword", "include_all", "include_any"):
                ui.input(f, value=data[f]).classes("w-full").props("dense outlined") \
                    .bind_value(data, f).tooltip(FIELD_HELP.get(f, ""))
                ui.label(FIELD_HELP.get(f, "")).classes("text-xs text-gray-400 -mt-2")
            for f in ("exclude_any", "warn_desc"):
                ui.textarea(f, value=data[f]).classes("w-full").props("dense outlined rows=3") \
                    .bind_value(data, f)
                ui.label(FIELD_HELP.get(f, "")).classes("text-xs text-gray-400 -mt-2")
            with ui.row().classes("w-full gap-3"):
                ui.number("价格下限 ¥", value=data["price_min"], format="%d") \
                    .props("dense outlined").bind_value(data, "price_min")
                ui.number("价格上限 ¥（0=不限）", value=data["price_max"], format="%d") \
                    .props("dense outlined").bind_value(data, "price_max")
                ui.number("捡漏线 %", value=data["deal_ratio"], format="%d") \
                    .props("dense outlined").bind_value(data, "deal_ratio") \
                    .tooltip("低于「成交中位数 × 此值%」时标捡漏。0=关闭")
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
            for k in ("price_min", "price_max", "deal_ratio"):
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
                store.update_rule(rule["id"], payload)
            else:
                store.insert_rule(payload)
            dlg.close()
            rules_view.refresh()
            hits_view.refresh()
            notify("已保存，下一轮生效")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=dlg.close).props("flat")
            ui.button("保存", on_click=save).props("color=primary")
    dlg.open()


def confirm_delete(rule: dict) -> None:
    with ui.dialog() as dlg, ui.card():
        ui.label(f"删除规则「{rule['name']}」？")
        ui.label("它抓到的商品和成交样本会一并删除，不可恢复。").classes("text-sm text-red-400")

        def do() -> None:
            store.delete_rule(rule["id"])
            dlg.close()
            rules_view.refresh()
            hits_view.refresh()
            notify("已删除")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=dlg.close).props("flat")
            ui.button("删除", on_click=do).props("color=negative")
    dlg.open()


@ui.refreshable
def rules_view() -> None:
    ui.button("新建规则", on_click=lambda: rule_dialog(None)).props("color=primary")
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
                ui.button("编辑", on_click=lambda r=rule: rule_dialog(r)).props("flat dense")
                ui.button("立即跑一轮", on_click=lambda r=rule: run_now(r)).props("flat dense")
                ui.button("删除", on_click=lambda r=rule: confirm_delete(r)
                          ).props("flat dense color=negative")


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
            comp = ui.number(it["k"], value=it["v"],
                             format="%d" if it["type"] == "int" else "%.1f") \
                .classes("w-full").props("dense outlined")
            fields[it["k"]] = (comp, it)
            ui.label(it["note"]).classes("text-xs text-gray-400")
            ui.label(f"默认值：{it['default']}").classes("text-xs text-gray-400")

    def save() -> None:
        bad = []
        for k, (comp, it) in fields.items():
            val = comp.value
            if val is None:
                bad.append(k)                  # 空值会让该项回落默认，多半是误删，拦一下
                continue
            store.save_setting(k, val)
        settings_view.refresh()
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

        with ui.tabs().classes("w-full") as tabs:
            t_hit = ui.tab("命中")
            t_all = ui.tab("全部")
            t_rule = ui.tab("规则")
            t_set = ui.tab("设置")
        with ui.tab_panels(tabs, value=t_hit).classes("w-full"):
            with ui.tab_panel(t_hit):
                ui.button("刷新", on_click=hits_view.refresh).props("flat dense")
                hits_view()
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
                rules_view()
            with ui.tab_panel(t_set):
                settings_view()
