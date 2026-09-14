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


def compose(rule: dict, row: dict, median: int | None) -> str:
    """一条提醒的正文。手机通知栏只看得见前两行，所以最要紧的信息必须在最前面。"""
    head = "🟢 捡漏" if row["is_deal"] else "命中"
    lines = [f"{head} | {rule['name']}", row["name"][:60], f"¥{row['price']:,}"]
    if row.get("deal_pct"):
        tail = f"（市价的 {row['deal_pct']}%"
        tail += f"，中位 ¥{median:,}）" if median else "）"
        lines[2] += tail
    if row.get("bid_count") is not None:
        # 拍卖的"当前价"只在此刻成立。不写这句，推送就是在误导人 ——
        # 和面板上那条竞价提示是同一个理由。
        lines.append(f"🔨 拍卖 · 已 {row['bid_count']} 次出价 · 还会涨")
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


def post(url: str, template: str, text: str, thumb: str = "") -> None:
    """发一条。失败只记日志，绝不向上抛 —— 推送坏了不该影响抓取。"""
    if template.strip():
        r = httpx.post(url, content=render(template, text, thumb).encode(),
                       headers={"content-type": "application/json", "user-agent": UA},
                       timeout=TIMEOUT)
    else:
        r = httpx.post(url, content=text.encode(),
                       headers={"content-type": "text/plain; charset=utf-8", "user-agent": UA},
                       timeout=TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")


def push_new(rule: dict) -> int:
    """把这条规则里还没推过的命中商品推出去，返回实际推送条数。

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

    rows = store.pending_notify(rule["id"], only_deal=(s.get("notify_on") != "matched"))
    if not rows:
        return 0

    cap = s.get("notify_max_per_round") or 5
    if len(rows) > cap:
        store.mark_notified(rows)
        log.warning("规则「%s」有 %d 件待推，超过上限 %d —— 本轮【不补推】，全部标为已推。"
                    "（多半是刚开启推送、刚重启、或刚放宽了规则）后面新出现的会正常推。",
                    rule["name"], len(rows), cap)
        return 0

    median = store.get_state(rule["id"])["median_price"]
    template = s.get("notify_body") or ""
    sent = 0
    for i, row in enumerate(rows):
        if i:                               # 第一条不用等
            time.sleep(GAP)
        try:
            post(url, template, compose(rule, row, median), row.get("thumb_url") or "")
            sent += 1
        except Exception as e:              # noqa: BLE001 - 推送坏了不该影响抓取
            # 【失败也标已推，不重试】一条迟到一小时的提醒没有意义，
            # 而对着挂掉的地址每轮重试会一直拖慢抓取，还把日志刷满。
            log.warning("推送失败（本条不再重试）：%s", e)
    store.mark_notified(rows)
    if sent:
        log.info("规则「%s」推送 %d 条", rule["name"], sent)
    return sent
