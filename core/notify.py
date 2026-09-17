"""把命中的商品推到你手机上。notify_url 留空＝整个功能关闭，一个请求都不发。

【为什么这个项目非有推送不可】这不是"同类项目都有所以跟着做"。
本项目自己抓到的数据：RTX 5090 成交中位数 ¥78 万，而在售最低 ¥93 万 ——
¥26 万〜93 万这一整段在售是空的。意思是好价的卡挂出来就被买走了，
它们从来没在面板上停留到你下次打开的那一刻。一个要人主动去刷的页面，
抓不到它本来就是为了抓的东西。

【为什么是通用 webhook，而不是接 Telegram / Discord / Bark 里的某一家】
接哪一家是你的事，不该由代码替你定。URL + 请求体模板这两项一填，
下面这些全都能用（都是实际的接口形状，不是设想）：

  ntfy      https://ntfy.sh/你的主题                       模板留空，发纯文本
  Bark      https://api.day.app/你的KEY                    模板留空，发纯文本
  Discord   https://discord.com/api/webhooks/...           {"content": "{text}"}
  企业微信  https://qyapi.weixin.qq.com/...                {"msgtype":"text","text":{"content":"{text}"}}
  Telegram  https://api.telegram.org/bot<TOKEN>/sendMessage {"chat_id":"123","text":"{text}"}

所以这里不写任何一家的适配代码，也就没有哪家改了接口要跟着修的问题。
"""
import json
import logging
import time

import httpx

import config
import sources
from db import store

log = logging.getLogger(__name__)

TIMEOUT = 5.0                 # 秒。推送卡住不能拖慢抓取，这是主功能
UA = "Resale-Watcher"
# 【两条推送之间必须隔一下】Slack 对每个 incoming webhook 限【1 条/秒】，
# 超了回 429。而本函数【失败不重试】—— 两条规矩撞在一起就是：一轮推 5 条，
# 后面几条直接 429，那几件捡漏你永远不会知道，日志里只有一行 warning。
# 1.2 秒是给 1 条/秒 留的余量。代价是一轮最多多花 5 秒，
# 而一轮扫描本来就要几分钟，这点时间看不出来。
# 其它家（ntfy/Bark/Discord/Telegram）的限额都比这宽松，按最严的来就都安全。
GAP = 1.2                     # 秒


def deadline(end) -> str:
    """拍卖的截止，写成 " · 剩 3 小时（09-14 20:33 截止）"。没有截止时间就是空串。

    【剩余时间和绝对时间必须都写】推送是一条躺在通知栏里的静止消息，
    而面板是打开就重算的。"剩 3 小时"只在发出去的那一秒成立 ——
    你半夜翻到这条时它可能早就结束了，而消息本身还理直气壮地写着"剩 3 小时"。
    绝对时间则永远读得对，但光有它又得自己心算还剩多久，
    而"到点就没了"要的正是扫一眼就有的紧迫感。两个都给，各补对方的短板。
    """
    if not end:
        return ""
    return f" · {config.time_left(end)}（{end:%m-%d %H:%M} 截止）"


def compose(rule: dict, row: dict, median: int | None, final: bool = False) -> str:
    """一条提醒的正文。手机通知栏只看得见前两行，所以最要紧的信息必须在最前面。

    【价格在第一行】原先第一行是「🟢 捡漏 | 规则名」、价格在第三行 —— 通知栏里
    正好看不到那个决定你要不要点开的数。现在第一行就是「头 + 价格 + 为什么算」。

    【手动价判出来的捡漏，理由要写"低于手动线"】它的市价百分比可能是 114%，
    写成「捡漏 …市价的 114%」在手机上读起来自相矛盾 —— 百分比只当参考放第三行。

    final=True 是「快结束提醒」，也就是这件商品的第二条；再跌够 notify_redrop_pct
    的是「📉 再降」。头一个词必须和第一条不一样：其余部分几乎一模一样，不换的话
    你会以为是重复推送、顺手划掉。
    """
    price, pct = row["price"], row.get("deal_pct")
    manual = rule.get("deal_price") or 0
    redrop = bool(row.get("notified_price")) and row["notified_price"] > price
    if final:
        head = "⏰ 捡漏快结束"
    elif redrop:
        head = "📉 再降"
    elif row["is_deal"]:
        head = "🟢 捡漏"
    else:
        head = "命中"

    if redrop:
        why = f"比上次推送低 ¥{row['notified_price'] - price:,}"
    elif row["is_deal"] and manual:
        why = f"低于手动线 ¥{manual:,}"
    elif pct:
        why = f"市价的 {pct}%"
    else:
        why = ""
    lines = [f"{head} ¥{price:,}" + (f" · {why}" if why else "") + f" | {rule['name']}",
             row["name"][:60]]

    ref = []
    if (redrop or (row["is_deal"] and manual)) and pct:
        ref.append(f"市价的 {pct}%")
    if median:
        ref.append(f"中位 ¥{median:,}")
    if ref:
        lines.append(" · ".join(ref))
    if row.get("bid_count") is not None:
        # 拍卖的"当前价"只在此刻成立。不写这句，推送就是在误导人 ——
        # 和面板上那条竞价提示是同一个理由。
        lines.append(f"🔨 拍卖 · 已 {row['bid_count']} 次出价 · 还会涨"
                     f"{deadline(row.get('end_time'))}")
    src = sources.get(row["source"])
    lines.append(src.item_url(row["item_id"]) if src else "")
    return "\n".join(lines)


# 模板里带 {thumb}、而这件商品又没有缩略图时，退回这个形状。
# 【为什么必须有兜底】Slack 对 image_url 为空串回的是 400 invalid_blocks，
# 整条消息直接发不出去 —— 而本模块失败不重试，那件捡漏就永久丢了。
# 实测库里 637 件商品三个源都给了缩略图、一件不缺，所以这条路平时走不到；
# 它防的是哪天某个源改了返回、或者新加一个源不给图。
# 形状用最小的 {"text": …}：Slack 和 Discord 都认。用别家的话自己确认一下。
NO_THUMB_FALLBACK = '{"text": "{text}"}'


def esc(v: str) -> str:
    """转义成能塞进 JSON 字符串字面量的样子。

    【必须转义】标题里带一个双引号或反斜杠就能把 JSON 撑破 —— 而日文商品名里
    引号并不罕见（「新品」"未開封" 之类）。直接字符串替换的话，推送会静默
    失败在对方的 400 上，日志里只看得到一个没头没尾的状态码。
    """
    return json.dumps(v, ensure_ascii=False)[1:-1]


def render(template: str, text: str, thumb: str = "") -> str:
    """把正文和缩略图地址填进 JSON 模板。

    【模板要 {thumb} 而商品没有图时，整个换成兜底模板】只把 {thumb} 填成空串
    是不行的：那样发出去的是 "image_url": ""，Slack 回 400，整条消息丢掉。
    """
    if "{thumb}" in template and not thumb:
        template = NO_THUMB_FALLBACK
    return template.replace("{text}", esc(text)).replace("{thumb}", esc(thumb))


class PushFailed(RuntimeError):
    """推送没发出去。transient=True 是瞬时的（429 / 5xx / 网络），下一轮该再试；
    False 是地址本身有问题（404 / 401 / 400 模板错），再试也是一样，标已推别再烦。"""

    def __init__(self, msg: str, transient: bool):
        super().__init__(msg)
        self.transient = transient


def post(url: str, template: str, text: str, thumb: str = "") -> None:
    """发一条。失败抛 PushFailed；调用方决定标不标已推。"""
    try:
        if template.strip():
            r = httpx.post(url, content=render(template, text, thumb).encode(),
                           headers={"content-type": "application/json", "user-agent": UA},
                           timeout=TIMEOUT)
        else:
            r = httpx.post(url, content=text.encode(),
                           headers={"content-type": "text/plain; charset=utf-8", "user-agent": UA},
                           timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise PushFailed(f"网络错误：{e}", transient=True) from e
    if r.status_code >= 400:
        raise PushFailed(f"HTTP {r.status_code} {r.text[:120]}",
                         transient=(r.status_code == 429 or r.status_code >= 500))


def push_new(rule: dict) -> int:
    """把这条规则里该推的商品推出去，返回实际推送条数。

    推两种：新命中（每件一次），以及【拍卖快结束提醒】—— 一件捡漏拍卖进入
    最后 notify_final_min 分钟、价格还在捡漏线内时的第二条，同样每件一次。

    【提醒必须在 mark_deals 之后跑】它挑的是 is_deal = 1，而 is_deal 是那边
    用最新价格重算出来的。反过来的话，提醒看到的是上一轮的捡漏判定 ——
    一件已经被别人抬出捡漏线的拍卖，会催着你去出价。

    【积压不补推】一次要推的超过 notify_max_per_round 就一条都不发，
    只把它们标成已推。这一条规则同时挡住三种炸弹：
      刚填上 notify_url —— 库里几十件老命中会一次性全轰出来
      停机几天后重启   —— 攒下的一堆过期货没有任何价值
      规则放宽/改价    —— 一改 price_max 可能几百件同时变成命中
    代价是真有一波好货时会被整体跳过，但那种情况面板上看得到，
    而被几十条通知淹掉的人只会直接关掉推送。
    """
    s = store.get_settings()
    url = (s.get("notify_url") or "").strip()
    if not url:
        return 0

    rows = store.pending_notify(rule["id"], only_deal=(s.get("notify_on") != "matched"),
                                redrop_pct=s.get("notify_redrop_pct") or 0)
    mins = s.get("notify_final_min") or 0
    final = store.pending_final(rule["id"], mins) if mins else []

    cap = s.get("notify_max_per_round") or 5
    if len(rows) > cap:
        store.mark_notified(rows)
        log.warning("规则「%s」有 %d 件待推，超过上限 %d —— 本轮【不补推】，全部标为已推。"
                    "（多半是刚开启推送、刚重启、或刚放宽了规则）后面新出现的会正常推。",
                    rule["name"], len(rows), cap)
        rows = []
    # 【快结束提醒单独判上限，不和上面合在一起算】合着算的话，一次放宽规则
    # 带来的几十件新命中会把同一轮里那条「还剩 20 分钟」一起吞掉 ——
    # 而那正是这个提醒存在的全部理由。它自己井喷不起来：能进这张表的只有
    # 「此刻正处在最后 N 分钟里的捡漏拍卖」，天然就是个位数。
    if len(final) > cap:
        store.mark_final_notified(final)
        log.warning("规则「%s」有 %d 件拍卖同时进入最后 %d 分钟，超过上限 %d —— 全部跳过。"
                    "（多半是刚把这个提醒打开）", rule["name"], len(final), mins, cap)
        final = []
    if not rows and not final:
        return 0

    median = store.get_state(rule["id"])["median_price"]
    template = s.get("notify_body") or ""
    sent = 0
    # 【两批合成一个队列再发】GAP 是按"上一条发出去多久"算的，分两个循环的话
    # 第二批的头一条不会等，两条会挤在同一秒里出去 —— 正好撞上 Slack 的 1 条/秒。
    queue = [(r, False) for r in rows] + [(r, True) for r in final]
    retry_later: list = []                  # 瞬时失败的：这一轮不标，下一轮再试
    for i, (row, is_final) in enumerate(queue):
        if i:                               # 第一条不用等
            time.sleep(GAP)
        try:
            post(url, template, compose(rule, row, median, final=is_final),
                 row.get("thumb_url") or "")
            sent += 1
        except Exception as e:              # noqa: BLE001 - 推送坏了不该影响抓取
            # 【地址本身坏了（404/401/400）：标已推、不重试】一条迟到一小时的提醒没有意义，
            # 而对着挂掉的地址每轮重试会一直拖慢抓取，还把日志刷满。
            # 【瞬时的（429 / 5xx / 网络抖动）：不标，下一轮再试】Slack 抽风五分钟
            # 就把那件捡漏永久丢掉，太亏；每轮最多 notify_max_per_round 条，重试不会失控。
            who = f"[{row['source']} {row['item_id']}] {row['name'][:40]}"
            if getattr(e, "transient", False):
                retry_later.append(id(row))
                log.warning("推送失败（瞬时，下一轮再试）%s：%s", who, e)
            else:
                log.warning("推送失败（本条不再重试）%s：%s", who, e)
    store.mark_notified([r for r in rows if id(r) not in retry_later])
    store.mark_final_notified([r for r in final if id(r) not in retry_later])
    if sent:
        log.info("规则「%s」推送 %d 条%s", rule["name"], sent,
                 f"（含快结束提醒 {len(final)} 条）" if final else "")
    return sent
