"""轮询编排。

一条规则 × 一个数据源，一轮做三件事（都只发「翻页搜索 + 少量详情」两种请求）：

  1. 扫在售   把该关键词在这个源上的在售商品全扫一遍，逐件判定后写库。
              实测「RTX 5090」在 Mercari 有 188 件、Yahoo!フリマ 108 件，
              各自 1〜2 页就扫完，所以每轮都是全量 —— 新上架、降价、售出一次全抓到。
  2. 售出对账 库里还标着在售、但这轮没扫到的，去核实是卖了还是下架了。
  3. 补详情   初筛命中但还没读过描述的，拉详情打警示标签（不否决）。

扫完所有源之后，再做两件【跨源】的事：
  4. 重判     拿当前规则把这条规则名下【所有源】的商品重判一遍（纯 CPU，零请求）。
  5. 算捡漏   用跨源合并的成交中位数给命中商品标百分比。

【为什么 4、5 要跨源】市价是「这东西值多少钱」，和它挂在哪个网站无关。
两边都是日本二手市场，合并样本量更大也更稳。
"""
import logging
import random
from datetime import timedelta

import config
import sources
from core import notify
from core.matcher import flag_desc, is_deal, judge_snap
from db import store

log = logging.getLogger("poller")

# 描述没读到时写进 desc_warn 的标记。和真实的警示词区分开，面板上单独显示。
DESC_UNREAD = "(描述未读到)"

# 轮询心跳。面板靠它判断「采集是不是还活着」——
# 没有它的话，线程静默死掉后进程照常在跑、面板照常打开、旧数据照常显示，
# 只有「上次扫描」那个时间戳冻在死亡那一刻，而界面上没有任何地方说这不正常。
_beat: dict = {"at": None}


def heartbeat():
    """轮询主循环最近一次转动的时间。None＝还没开始转。"""
    return _beat["at"]


def _due(last, interval_min: float) -> bool:
    """到点了没。间隔上抖 ±20%，免得每条规则都卡在整分钟上发请求。"""
    if last is None:
        return True
    return config.now() - last >= timedelta(minutes=interval_min * random.uniform(0.8, 1.2))


# ------------------------------------------------------------------ 1+2. 扫在售

def scan_on_sale(src, rule: dict) -> dict:
    rid = rule["id"]
    seen: set[str] = set()
    stat = {"total": 0, "new": 0, "price_down": 0, "pages": 0}
    token = ""

    for _ in range(rule["max_pages"]):
        page = src.search(rule["keyword"], page_token=token)
        stat["total"] = page["total"]
        stat["pages"] += 1
        for snap in page["items"]:
            # 【seen 必须在过滤之前加】seen 回答的是"这轮搜索有没有见到它"，
            # 和"要不要收它"是两件事。原先放在 keep 判断之后，于是你一收窄
            # include_all，那些还挂在库里的老商品就既不更新 last_seen_at、
            # 也不进 seen —— 被售出对账当成失踪，每轮白烧一个详情请求确认
            # "它还在售"，然后什么都不做，下轮重来，永不收敛。
            seen.add(snap["item_id"])
            verdict = judge_snap(rule, snap)
            if not verdict["keep"]:
                continue                    # 关键词根本不匹配，按基线约定不入库
            res = store.upsert_item(rid, snap, verdict)
            if res["new"]:
                stat["new"] += 1
            elif res["price_changed"] and snap["price"] < res["old_price"]:
                stat["price_down"] += 1
                log.info("[%s] 降价 %s ¥%s → ¥%s  %s", src.key, snap["item_id"],
                         f"{res['old_price']:,}", f"{snap['price']:,}", snap["name"][:40])
        token = page["next"]
        if not token:
            break

    truncated = bool(token)
    if truncated:
        log.warning("[%s] 规则「%s」在售 %d 件，%d 页没扫全 —— 本轮跳过售出对账"
                    "（没扫全时「没出现」不等于「卖掉了」）。到设置页调大 max_pages 或收窄关键词。",
                    src.key, rule["name"], stat["total"], rule["max_pages"])

    store.update_source_state(rid, src.key, last_scan_at=config.now(),
                              last_total=stat["total"], truncated=int(truncated))
    if not truncated:
        reconcile_sold(src, rule, seen)
    return stat


def reconcile_sold(src, rule: dict, seen: set[str]) -> None:
    """库里还标在售、这轮却没扫到的商品：卖了？下架了？还是只是漏翻了一页？"""
    rid = rule["id"]
    cutoff = config.now() - timedelta(minutes=rule["missing_grace_min"])
    missing = [it for it in store.on_sale_items(rid, src.key)
               if it["item_id"] not in seen and it["last_seen_at"] < cutoff]

    budget = rule["detail_budget"]
    checked = 0
    for it in missing:
        if budget <= 0:
            break
        iid = it["item_id"]
        budget -= 1
        checked += 1
        d = src.detail(iid)
        if d is None:
            store.set_status(src.key, iid, rid, "gone")          # 已删除
            continue
        # 【无论核实出什么，都算"刚见过"】不 touch 的话 last_seen_at 永远停在旧值，
        # 下一轮它照样满足 missing 条件、照样花一个详情请求、照样什么都判不出来 ——
        # 攒够 detail_budget 个这种僵尸就会把预算吃光，真正卖掉的商品永远轮不到核实，
        # 最后烧穿每日配额导致全源停抓。审查里有 8 条独立发现都指向这一处。
        store.touch_seen(src.key, iid, rid)
        if d["status"] == "sold_out":
            now = config.now()
            store.set_status(src.key, iid, rid, "sold_out", sold_at=now)
            if it["matched"]:
                # 我们一路跟到成交的商品：拉过详情、过了完整规则，是最可信的市价样本
                store.add_sold_sample(rid, src.key, iid, d["price"], now, "tracked")
            log.info("[%s] 售出 %s ¥%s", src.key, iid, f"{d['price']:,}")
        elif d["status"] == "trading":
            store.set_status(src.key, iid, rid, "trading")
        elif d["status"] == "gone":
            # ヤフオク 的流标拍卖（结束了但一次出价都没有）。漏掉这个分支的话，
            # 它既不进 sold_out 也不进 trading，落到最后什么都不做 —— 而它
            # 再也不会出现在在售搜索结果里，于是每过一个 missing_grace_min
            # 就重新进 missing 队列、再花一个详情请求、再什么都判不出来，
            # 和上一轮修掉的僵尸死循环是同一类，只是走的另一个 status 取值。
            store.set_status(src.key, iid, rid, "gone")
        elif not d["status"]:
            # 详情页读到了但解析不出状态（页面改版/异常页）。不猜、不改状态，
            # 但上面已经 touch 过，所以不会变成每轮重来的死循环。
            log.warning("[%s] %s 详情读不出状态，保持原状下轮再看", src.key, iid)
        # 还在售 → 只是这轮没翻到，已 touch，下个宽限期再说

    # 【这一行是本项目最大的一笔请求开销，必须可见】实测 4 条规则稳态下，
    # 对账核实占全部请求的六成，比搜索本身还多 —— 而它原先一行日志都不打。
    # 原因见上面那句「还在售 → 下个宽限期再说」：库里还标在售、却不在搜索结果里的
    # 商品（卖家改了标题、掉出关键词、或搜索排序把它挤走），每过一个
    # missing_grace_min 就要重新核实一次，永远循环。这是刻意的（为了逮到它哪天真卖掉），
    # 但你调 daily_request_limit 之前得知道钱花在这儿。
    if checked:
        log.info("[%s] 规则「%s」对账核实 %d 件（失踪 %d 件，预算 %d）",
                 src.key, rule["name"], checked, len(missing), rule["detail_budget"])


# ------------------------------------------------------------------ 3. 补详情

def fetch_details(src, rule: dict) -> int:
    """给初筛命中但没读过描述的商品拉详情，用 warn_desc 扫一遍打警示标签。

    【不会因此把商品毙掉】标签只是提示，商品照样留在命中列表里。
    """
    if not rule.get("check_desc"):
        return 0
    rid = rule["id"]
    done = 0
    for it in store.pending_detail(rid, src.key, rule["detail_budget"]):
        iid = it["item_id"]
        d = src.detail(iid)
        if d is None:
            store.set_status(src.key, iid, rid, "gone")
            continue
        if d["description"] is None:
            # 详情页打开了，但描述没解析出来（页面结构变了）。
            # 【仍然置 desc_checked】否则每轮都会重试这批商品，把 detail_budget 吃光；
            # 但 desc_warn 写明「读取失败」——绝不能让面板显示成「描述已查，干净」，
            # 那等于用一条假信息盖住了整个警示层已经失效的事实。
            store.save_detail(src.key, iid, rid, "", DESC_UNREAD, d.get("ship_from", ""))
            done += 1
            log.warning("[%s] %s 描述没解析出来（页面结构可能变了）", src.key, iid)
            continue
        warn = flag_desc(rule, d["description"])
        store.save_detail(src.key, iid, rid, d["description"], warn, d.get("ship_from", ""))
        done += 1
        if warn:
            log.info("[%s] 描述警示 %s [%s] %s", src.key, iid, warn, it["name"][:40])
    return done


# ------------------------------------------------------------------ 4. 重判（跨源）

def revalidate(rule: dict) -> int:
    """拿当前规则把这条规则名下【所有源】的商品全部重判一遍。不发请求，纯 CPU。

    存在的意义是让规则改动立刻覆盖历史数据：你在面板上删掉一个排除词，
    下一轮那些被它误杀的商品就会自己回到命中列表 —— 否则它们会永远
    带着旧的 reject_reason 躺在库里，而你根本不会想起去翻。
    """
    rid = rule["id"]
    changed = 0
    for r in store.query(
            # 【这个列表必须覆盖 judge_snap 读的每一个字段】漏一个的后果是静默的：
            # 扫描时用完整 snap 判成不合适，紧接着 revalidate 拿缺字段的行判回合适，
            # 两个动作在同一轮里一前一后互相抵消 —— 那条规则看起来就是"不生效"，
            # 而日志和面板都不会有任何异常。seller_id 就是这么漏掉过一次的。
            "SELECT source, item_id, name, price, item_type, condition_id, seller_id, "
            "matched, reject_reason, desc_checked, desc_warn, description "
            "FROM item WHERE rule_id = %s",
            (rid,)):
        v = judge_snap(rule, r)
        if r["desc_warn"] == DESC_UNREAD:
            # 【读失败是终态，重算不了】这一行的 description 存的是空串（详情页打开了
            # 但描述没解析出来），拿它去跑 flag_desc 必然得到空 —— 于是每一轮
            # revalidate 都会把 fetch_details 刚写下的 DESC_UNREAD 洗掉，
            # 面板转而显示「✓ 描述已查，干净」。那正是用一条假信息盖住
            # 「警示层对这件商品整个失效」的事实，也是 matcher/yahoo_flea/poller
            # 三处注释都写明绝不能发生的那件事。
            warn = DESC_UNREAD
        elif r["desc_checked"]:
            # 描述已经在库里了，警示标签一起重算 —— 改了 warn_desc 下一轮就反映出来
            warn = flag_desc(rule, r["description"])
        else:
            warn = ""
        if (v["matched"] != r["matched"] or v["reject_reason"] != r["reject_reason"]
                or warn != r["desc_warn"]):
            store.execute("UPDATE item SET matched = %s, reject_reason = %s, desc_warn = %s "
                          "WHERE source = %s AND item_id = %s AND rule_id = %s",
                          (v["matched"], v["reject_reason"], warn,
                           r["source"], r["item_id"], rid))
            changed += 1
    return changed


# ------------------------------------------------------------------ 5. 捡漏（跨源）

def mark_deals(rule: dict) -> None:
    """用跨源合并的成交中位数给命中商品打百分比。不发请求。"""
    rid = rule["id"]
    median = store.get_state(rid)["median_price"]
    for it in store.query(
            "SELECT source, item_id, price FROM item WHERE rule_id = %s AND matched = 1 "
            "AND status = 'on_sale'", (rid,)):
        deal, pct = is_deal(it["price"], median, rule["deal_ratio"], rule.get("deal_price") or 0)
        store.set_deal(it["source"], it["item_id"], rid, deal, pct)


# ------------------------------------------------------------------ 成交轮

def scan_sold(src, rule: dict) -> dict:
    """抓成交价样本。样本入库时带上来源，但中位数是跨源合并算的。

    【这里刻意去掉价格上限】price_max 是「你的预算」，不是「这东西值多少钱」。
    拿预算上限去裁成交样本，中位数必然落在预算内 —— 那它就失去了参考意义，
    变成了循环论证。price_min 则保留：低于它的多半是配件和废品，留着会把中位数压垮。
    """
    rid = rule["id"]
    # 【先写时间戳再干活】写在末尾的话，成交轮中途抛异常（限流/解析出错/对方 500）
    # 就永远不会落盘，_due 恒为 True，于是每 30 秒的 tick 都先重跑一遍成交轮、
    # 再失败、再跳过在售扫描 —— 主功能（新上架监控）永久停摆，配额被热重试烧光。
    # 成交价不是紧急数据，失败就等下一个周期，比无限重试安全得多。
    store.update_source_state(rid, src.key, last_sold_at=config.now())
    price_rule = dict(rule, price_max=0)
    token, added = "", 0
    # 各家的成交检索都不是按【成交时间】排序的，翻回来的样本时间跨度很大
    # （实测 Mercari 5 页 331 件里只有 15 件是近 30 天成交的）。窗口外的直接跳过：
    # 写进去也会被 prune_sold_samples 立刻删掉，白写一趟。
    cutoff = config.now() - timedelta(days=rule["median_window_days"])

    for _ in range(rule["max_pages"]):
        page = src.search(rule["keyword"], sold=True, page_token=token)
        for snap in page["items"]:
            if not judge_snap(price_rule, snap)["matched"]:
                continue
            # 【取不到成交时间就跳过，不要当成"刚刚成交"】原先 `or config.now()`
            # 是 fail-open：解析不出时间的样本会直接穿透 30 天窗口过滤，
            # 把不知道多久以前的成交价塞进中位数里污染基准。宁可少一个样本。
            sold_at = snap["updated_at_src"]
            if sold_at is None or sold_at < cutoff:
                continue
            # 成交样本只过了标题级规则（没拉描述）—— 几百件成交品逐个拉详情不现实。
            # sample_kind='scan' 就是在标记这一点；我们自己跟到成交的那些是 'tracked'，更准。
            store.add_sold_sample(rid, src.key, snap["item_id"], snap["price"], sold_at, "scan")
            added += 1
        token = page["next"]
        if not token:
            break

    store.prune_sold_samples(rid)
    median, n = store.refresh_median(rid)
    log.info("[%s] 规则「%s」成交样本 +%d；跨源近%d天共 %d 件，中位数 %s",
             src.key, rule["name"], added, rule["median_window_days"], n,
             f"¥{median:,}" if median else f"样本不足{rule['median_min_samples']}件，暂不出数")
    return {"added": added, "median": median, "samples": n}


# ------------------------------------------------------------------ 追踪刷新

def refresh_tracked() -> int:
    """把追踪中的商品单独拉一遍详情。返回实际刷新的件数。

    【这是全项目唯一按件发请求的地方】平时价格和状态只在整轮关键词扫描时更新
    （每条规则 quick_min，而且一轮要遍历所有源）；追踪的商品直接拉它自己的详情页，
    所以快得多 —— 拍卖的出价数也只有这条路能及时拿到，而那恰恰是你盯一件拍卖的理由。

    【两道闸】track_min 控间隔、track_budget 控每轮件数。少了任何一道，
    追十几件就能把每日配额烧穿，而配额一满是【所有规则所有源】一起停。

    【不跨规则去重】同一件商品被两条规则抓到就是两行，各追各的、各发各的请求。
    看着浪费，但合并的话「这件在这条规则下算不算命中」就得引入额外的关联表，
    不值当 —— 真追到重复的，你自己取消一个就行。
    """
    s = store.get_settings()
    rows = store.tracked_due(s["track_min"], s["track_budget"])
    if not rows:
        return 0
    done = 0
    for r in rows:
        src = sources.get(r["source"])
        if src is None:
            continue
        try:
            d = src.detail(r["item_id"])
        except sources.DailyLimitReached:
            raise                       # 配额耗尽要一路抛到主循环，停掉所有活儿
        except Exception as e:          # noqa: BLE001 - 单件失败不该拖垮整批
            log.warning("[%s] 追踪刷新 %s 失败：%s", r["source"], r["item_id"], e)
            continue
        done += 1
        if d is None:                   # 404，商品被删了
            store.set_status(r["source"], r["item_id"], r["rule_id"], "gone")
            log.info("[%s] 追踪中的 %s 已下架", r["source"], r["item_id"])
            continue
        # 【无论详情读出什么都要 touch】不 touch 的话 last_seen_at 停在旧值，
        # 下一轮它照样到点、照样白烧一个请求 —— 和售出对账那边是同一个坑。
        store.touch_seen(r["source"], r["item_id"], r["rule_id"])
        store.update_tracked(r["source"], r["item_id"], r["rule_id"], d, r["price"])
        if d["status"] in store.TERMINAL:
            log.info("[%s] 追踪中的「%s」%s ¥%s —— 已自动取消追踪，去「成交」页看",
                     r["source"], r["name"][:30],
                     "卖掉了" if d["status"] == "sold_out" else "下架了", f"{d['price']:,}")
    if done:
        log.info("追踪刷新 %d 件（追踪中共 %d 件，每轮上限 %d）",
                 done, len(store.tracked_items()), s["track_budget"])
    return done


# ------------------------------------------------------------------ 主循环

def finalize(rule_id: int) -> dict:
    """一轮扫完之后的跨源收尾：重判 → 算捡漏 → 推送。返回 {'rejudged', 'notified'}。

    【必须在这里重新取规则，不能用调用方手上那份】传进来的 rule 是一轮开始时
    取的快照，而一轮要跑几十秒到几分钟（每个请求之间还有 3~8 秒的随机等待）。
    你在这期间于面板上点了「拉黑卖家」，拿旧快照跑 revalidate 会把刚拉黑的商品
    原样判回命中 —— 用户最明确的一次主动操作被静默回滚，面板上那几件商品
    当着人的面又冒出来，而日志里什么都没有。

    【扫描阶段用旧快照是可以接受的】scan_on_sale 期间 upsert 可能按旧规则
    写回 matched=1，但紧接着这里就用新规则整体重判一遍，同一轮内就收敛了。

    【推送必须挂在这里，不能只挂在 run_once 上】常驻轮询走的是 _run_round，
    面板的「立即跑一次」才走 run_once。推送只挂后者的话，正常部署方式
    （./run.sh start / systemd）下一条都发不出去，而且待推的商品会一直
    堆到某次手动触发时超过 notify_max_per_round，被整批标成已推、永久丢掉。
    """
    rule = store.get_rule(rule_id)
    if rule is None:
        return {"rejudged": 0, "notified": 0}       # 规则在这一轮里被删了
    out = {"rejudged": revalidate(rule), "notified": 0}
    mark_deals(rule)
    # 【推送在 mark_deals 之后】只推捡漏时，is_deal 要先算出来才知道该推谁。
    # 【单独包 try】推送是附属功能，它坏了不该让这一轮的抓取成果丢掉 ——
    # push_new 内部已经吞掉了单条发送的异常，这里兜的是读库、读设置这些。
    try:
        out["notified"] = notify.push_new(rule)
    except Exception as e:                  # noqa: BLE001 - 推送故障不该拖垮抓取
        log.warning("规则「%s」推送环节失败：%s", rule["name"], e)
    return out


def run_once(rule: dict) -> dict:
    """跑一条规则的完整一轮（遍历它启用的所有源）。面板上的「立即跑一次」也走这里。"""
    srcs = sources.for_rule(rule)
    agg = {"total": 0, "new": 0, "price_down": 0, "details": 0, "pages": 0, "notified": 0}

    for src in srcs:
        # 【每个源单独 try】否则第一个源被限流就会放弃剩下所有源，
        # 连跨源的重判/捡漏都跑不到 —— 一个源的故障扩散成整条规则停摆。
        try:
            stat = scan_on_sale(src, rule)
            stat["details"] = fetch_details(src, rule)
        except Exception as e:              # noqa: BLE001 - 单源故障不该拖垮整条规则
            log.warning("[%s] 规则「%s」本轮失败：%s", src.key, rule["name"], e)
            store.update_source_state(rule["id"], src.key, last_error=str(e)[:255])
            continue
        for k in ("total", "new", "price_down", "details", "pages"):
            agg[k] += stat.get(k, 0)
        log.info("[%s] 规则「%s」在售%d件/扫%d页 新增%d 降价%d 读描述%d",
                 src.key, rule["name"], stat["total"], stat["pages"],
                 stat["new"], stat["price_down"], stat["details"])

    agg.update(finalize(rule["id"]))
    agg["matched"] = store.one("SELECT COUNT(*) n FROM item WHERE rule_id = %s AND matched = 1 "
                               "AND status = 'on_sale'", (rule["id"],))["n"]
    log.info("规则「%s」合计：在售%d件 新增%d 降价%d 改判%d → 当前命中%d",
             rule["name"], agg["total"], agg["new"], agg["price_down"],
             agg["rejudged"], agg["matched"])
    return agg


def loop(stop_event) -> None:
    """常驻主循环。每 30 秒看一眼哪条规则的哪个源到点了，各自独立计时。

    【异常处理的三条规矩】都是踩出来的：
      1. 成交轮和在售扫描【分开 try】—— 在售扫描是主功能（新上架、降价），
         不能因为成交轮出错就整块跳过。
      2. 失败也要推进时间戳 —— 否则 _due 恒为 True，30 秒后立刻重试同一个
         正在限流的源，把配额烧光而一次都没成功。
      3. 每日配额耗尽要停【全部】规则，不是只跳出当前这层循环。
    """
    log.info("轮询启动，数据源：%s",
             "、".join(f"{s.name}({s.key})" for s in sources.all_sources().values()))
    while not stop_event.is_set():
        _beat["at"] = config.now()
        try:
            rules = store.get_rules(enabled_only=True)
        except Exception as e:                              # noqa: BLE001 - 库抽风不该让进程死掉
            log.error("读规则失败：%s", e)
            stop_event.wait(60)
            continue

        halted = False                      # 当天配额已耗尽：停掉所有规则所有源
        # 【整块包一层】下面 get_source_state / update_source_state 本身也会抛
        # （数据库短暂拒连就够了），而它们不在任何 try 里 —— 异常会逐层逃出
        # while 循环，daemon 线程静默死亡，此后再也不抓任何东西。
        # 【放在整轮扫描之前】一轮要遍历 4 条规则 × 3 个源、每个请求还要等 3~8 秒，
        # 跑满可能要几分钟。追踪刷新排在后面的话，"更新快"就无从谈起了。
        try:
            refresh_tracked()
        except sources.DailyLimitReached as e:
            log.warning("%s —— 今天不再发请求，等跨日", e)
            halted = True
        except Exception:                   # noqa: BLE001 - 追踪坏了不该拖垮主循环
            log.exception("追踪刷新出错，本轮跳过")

        try:
            # 【必须接住返回值】它返回「当天配额是否已耗尽」，原先这里丢掉了，
            # 于是 halted 恒为 False，下面那句「耗尽就睡 10 分钟」从来没生效过：
            # 配额满了之后照样每 30 秒空转一轮，每轮都把所有规则所有源重走一遍
            # 才在第一个请求处被 _check_quota 拦下。
            halted = _run_round(rules, stop_event) or halted
        except Exception:                   # noqa: BLE001 - 循环本身永不退出
            log.exception("轮询主循环出错，本轮跳过")
        stop_event.wait(600 if halted else 30)
    log.info("轮询停止")


def _run_round(rules, stop_event) -> bool:
    """跑一轮：遍历每条规则的每个源。异常由调用方兜住，保证主循环不死。

    返回 True 表示当天请求配额已耗尽，调用方应该睡久一点再回来。
    """
    halted = False
    for rule in rules:
        if stop_event.is_set() or halted:
            break
        rid, worked = rule["id"], False

        for src in sources.for_rule(rule):
            if stop_event.is_set() or halted:
                break
            st = store.get_source_state(rid, src.key)

            # —— 成交轮（可有可无，失败绝不能影响下面的在售扫描）——
            if _due(st["last_sold_at"], rule["sold_scan_hours"] * 60):
                try:
                    scan_sold(src, rule)
                    worked = True
                except sources.DailyLimitReached as e:
                    log.warning("%s —— 今天不再发请求，等跨日", e)
                    store.update_source_state(rid, src.key, last_error=str(e)[:255])
                    halted = True
                    break
                except Exception as e:  # noqa: BLE001
                    log.warning("[%s] 规则「%s」成交轮失败：%s", src.key, rule["name"], e)
                    store.update_source_state(rid, src.key, last_error=str(e)[:255])

            # —— 在售扫描（主功能）——
            if _due(st["last_scan_at"], rule["quick_min"]):
                try:
                    stat = scan_on_sale(src, rule)
                    stat["details"] = fetch_details(src, rule)
                    worked = True
                    store.update_source_state(rid, src.key, last_error="")
                    # 【常驻轮询也要留下这一行】这条汇总原先只写在 run_once 里，
                    # 而 run_once 只有面板的「立即跑一次」和 ./run.sh once 会走 ——
                    # 也就是说正常部署方式下，每轮扫了几页、拉了几个详情，
                    # 日志里一个字都没有，只有降价/警示这类异常事件才留痕。
                    # 结果是「今天这些请求到底花在哪了」根本查不了。
                    log.info("[%s] 规则「%s」在售%d件/扫%d页 新增%d 降价%d 读描述%d",
                             src.key, rule["name"], stat["total"], stat["pages"],
                             stat["new"], stat["price_down"], stat["details"])
                except sources.DailyLimitReached as e:
                    log.warning("%s —— 今天不再发请求，等跨日", e)
                    store.update_source_state(rid, src.key, last_error=str(e)[:255])
                    halted = True
                    break
                except sources.RateLimited as e:
                    # 客户端里已经退避睡过了。这里【必须推进 last_scan_at】，
                    # 否则 30 秒后又来一次，每次只为换回一个 429。
                    log.warning("[%s] 规则「%s」被限流：%s —— 推迟一个周期",
                                src.key, rule["name"], e)
                    store.update_source_state(rid, src.key, last_error=str(e)[:255],
                                              last_scan_at=config.now())
                except Exception as e:  # noqa: BLE001 - 单个源出错不该拖垮整个轮询
                    log.exception("[%s] 规则「%s」出错", src.key, rule["name"])
                    store.update_source_state(rid, src.key, last_error=str(e)[:255],
                                              last_scan_at=config.now())

        if worked:
            # 跨源的收尾：重判 + 捡漏 + 推送。只在真扫过东西时做，没动静就别空跑。
            try:
                finalize(rid)
            except Exception:           # noqa: BLE001 - 纯本地计算，出错记下继续
                log.exception("规则「%s」收尾失败", rule["name"])
    return halted
