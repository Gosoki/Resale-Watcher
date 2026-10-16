"""ヤフオク（Yahoo!オークション）。

【这是三个源里唯一没有 JSON 接口的】只能从网页里抠。

  搜索页（2026-10 起的新版）  Next.js 页面，结果在 `__NEXT_DATA__` 那坨 JSON 的
          props.pageProps.initialState.search.items.listing 里：
          { items: [{auctionId, title, price, bidCount, endTime, buyNowPrice,
                     isShoppingItem, isFleamarketItem, seller.userId, imageUrl, …}],
            totalResultsAvailable }
          【改版是悄无声息的】新页面上一个 `<li class="Product">` 都没有了，旧解析器
          照常返回「0 件、已到底」—— 上层就把库里所有在售商品当成失踪，每轮拉详情核实，
          9 小时白花 1595 个请求，新上架的一件没抓到。所以现在：
            · 先按已知路径找，找不到再按「形状」找（带 totalResultsAvailable、
              items 里是 auctionId 的那个容器），对方挪个位置不至于立刻瞎掉；
            · 两种都找不到才退回下面的旧版块解析；
            · 页面说有 N 件、我们却一件 ID 都拿不到 → 抛 SourceBroken，绝不当成"没货"。

  搜索页（旧版，作兜底）  每个商品是一个 `<li class="Product">`，块里带一整套 `data-auction-*` 属性
          （id / title / price / startprice / buynowprice / endtime / category / img /
            seller-id / isfreeshipping / isshoppingitem）。这些是语义化属性，
          比按 CSS class 定位稳得多 —— Yahoo 的 class 是构建哈希（形如
          `gv-Heading--kAIOU7uq68gIXh6WMpSy`），前端发一版就变，绝不能依赖。
          出价数例外，只能从 `Product__bid` 取（这个 class 在搜索页是稳定的）。

  详情页  状态/当前价/出价数/结束时间在 `__NEXT_DATA__` 的
          props.pageProps.initialState.item.detail.item 里；
          描述【不在】那坨 JSON 里，得从 HTML 里以「商品説明」这个标题文字为锚点切。
          锚点是界面文案不是构建哈希，相对可靠，但仍然是全项目最可能哪天悄悄坏掉的一处 ——
          所以提取失败一律返回空描述，绝不返回 None（那会被上层当成「商品没了」）。

【フリマ 商品要滤掉】ヤフオク 的搜索结果里混着 Yahoo!フリマ 的商品（新版是
`isFleamarketItem: true`，旧版块上是 `data-auction-isflea="1"`）。而 フリマ 我们有独立的源在抓
—— 不滤掉就是同一件商品进两个源：面板上出现两遍，成交样本也会被记两次把中位数带偏。
旧版默认排序恰好一件 フリマ 都不返回；2026-10 的新版默认就混着（rtx 4090：84 件里 35 件），
当初加这道保险时担心的「对方改个默认值」已经发生了。

【成交价不做】落札相場页（closedsearch）没有 data-auction-* 属性，只有构建哈希 class。
为多一个市价样本源去扛一个每次发版就坏的解析器不划算 —— ヤフオク 的商品用
メルカリ + Yahoo!フリマ 合并出来的市价中位数判捡漏，一样能用。
"""
import json
import re
from urllib.parse import quote

from sources.base import Source, SourceBroken, pref_of

SEARCH_URL = "https://auctions.yahoo.co.jp/search/search?p={kw}&n={n}&b={b}"
ITEM_PAGE = "https://page.auctions.yahoo.co.jp/jp/auction/{}"
PAGE_SIZE = 100

_NEXT_DATA = re.compile(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_BID = re.compile(r">\s*([\d,]+)\s*<")


class YahooAuction(Source):
    key = "yahoo_auction"
    name = "ヤフオク"

    def item_url(self, item_id: str) -> str:
        return ITEM_PAGE.format(item_id)

    def search(self, keyword: str, *, sold: bool = False, page_token: str = "") -> dict:
        if sold:
            # 见模块 docstring：落札相場页没有可靠的解析锚点，成交样本交给另外两个源
            return {"items": [], "next": "", "total": 0}

        begin = int(page_token or 1)
        html = self._call("GET", SEARCH_URL.format(kw=quote(keyword), n=PAGE_SIZE, b=begin)).text
        listing = _listing(html)
        if listing is not None:
            raw = [x for x in listing.get("items") or [] if isinstance(x, dict)]
            ids = [x for x in raw if x.get("auctionId")]
            items = [self._from_json(x) for x in ids if not x.get("isFleamarketItem")]
            total = _int(listing.get("totalResultsAvailable"))
        else:
            raw = _blocks(html)
            ids = [b for b in raw if _attr(b, "id")]
            items = [self._parse(b) for b in ids if not _attr(b, "isflea")]
            # 旧版页面只能从文本里抠「N件」，只拿来显示和下面的改版判断，不拿来终止翻页
            m = re.search(r"([\d,]+)件", html)
            total = int(m.group(1).replace(",", "")) if m else len(items)

        # 【页面说有货、我们一件 ID 都拿不到 ＝ 解析坏了，不是没货】
        # 只在"这一页本该有东西"时判：总数正好是 100 的整数倍时会多翻一页空页，那不算。
        # 判的是过滤【前】的 ID —— 一页全是 フリマ 商品时过滤后是 0 件，那是正常的。
        if not ids and total >= begin:
            raise SourceBroken(f"{self.name} 搜索页显示有 {total} 件，却一件都解析不出来"
                               "（多半是页面改版了）")

        # 【翻页终止靠"本页有没有装满"，不靠总数】旧版的总数是正则从文本里抠的，
        # 页面上任何一处「◯◯件」都可能先匹配上；拿它算终止条件，抠错就会漏页或死循环。
        # 装不满＝到底了，这个判断不会错。
        # 【按未过滤的条数推进】b= 是"从第几件开始"的偏移量，按过滤后的件数推进
        # 会让下一页从已经看过的位置重新开始，越滤越退回去。
        nxt = str(begin + len(raw)) if len(raw) >= PAGE_SIZE else ""
        return {"items": items, "next": nxt, "total": total}

    def detail(self, item_id: str) -> dict | None:
        resp = self._call("GET", ITEM_PAGE.format(item_id))
        if resp.status_code == 404:
            return None
        html = resp.text
        item = {}
        m = _NEXT_DATA.search(html)
        if m:
            try:
                blob = json.loads(m.group(1))
                item = ((blob.get("props") or {}).get("pageProps", {})
                        .get("initialState", {}).get("item", {}).get("detail", {}).get("item")) or {}
            except (json.JSONDecodeError, AttributeError):
                item = {}

        status = item.get("status") or ""
        bids = int(item.get("bids") or 0)
        if status == "open":
            mapped = "on_sale"
        elif status:
            # 拍卖结束：有人出过价就是成交，没人出价是流标（对我们都一样是"没了"，
            # 但成交的那件价格有参考价值，标成 sold_out 让它进市价样本）
            mapped = "sold_out" if bids > 0 else "gone"
        else:
            # 【解析不出状态时返回空串，不要猜】上层看到空串会保持原状、下轮再试；
            # 猜成 gone 会把一件还在拍的商品从列表里抹掉。
            mapped = ""

        return {
            "description": _extract_desc(html),
            # 【必须取 taxinPrice，不是 price】详情 JSON 里两个字段并存：
            #   price      = 318182  税抜
            #   taxinPrice = 350000  税込（taxRate=10）
            # 而搜索页的 data-auction-price 给的是【税込】。取错的话同一件商品
            # 从搜索切到详情就凭空掉 9%：追踪时误报「已降」、污染价格历史，
            # 更要命的是 reconcile_sold 用它存成交样本 —— ヤフオク 的每一条成交价
            # 都会低 9%，直接把市价中位数往下拽，而那是捡漏判定的全部依据。
            "price": int(item.get("taxinPrice") or item.get("price") or 0),
            "name": item.get("title") or "",
            "status": mapped,
            # seller.location.prefecture，已经是「東京都」这种日文写法
            # 【可能带市区町村】实测有卖家填「東京都 板橋区」，归一到都道府県
            "ship_from": pref_of((((item.get("seller") or {}).get("location") or {})
                                  .get("prefecture"))),
            # 【追踪拍卖主要就看这个数】出价数一涨说明有人在抢，当前价还会往上走。
            # 状态解析不出来时（mapped 为空）别给 0 —— 那会被读成「还没人出价」。
            "bid_count": bids if mapped else None,
        }

    def _from_json(self, x: dict) -> dict:
        """新版搜索页里的一件商品（listing.items 的一个元素）→ 和旧版 _parse 同口径的 snap。

        【一口价不用再换算】旧版块上商家品的 buynowprice 给的是税抜、要 ×110//100；
        新版的 buyNowPrice 已经是税込（另有 buyNowPriceNoTax）。实测同一件商家品：
        buyNowPrice=37800、buyNowPriceNoTax=34364，和库里旧解析换算出来的 37800 一致。
        price 是当前价，新旧版都是税込。
        【卖家 ID 新旧一致】seller.userId 和旧版 data-auction-auc-seller-id 是同一个值
        （实测库里 37 件逐一相等），拉黑列表不用迁移。
        """
        seller = x.get("seller") or {}
        end = self.iso(x.get("endTime"))
        bids = x.get("bidCount")
        return {
            "source": self.key,
            "item_id": str(x.get("auctionId") or "")[:32],
            "name": x.get("title") or "",
            "price": _int(x.get("price")),
            "status": "on_sale",
            # 新版其实给了 itemCondition（NEW / USED10 / USED20 …），但还没接 ——
            # 保持旧口径 None：品相白名单对本源不生效，不知道 ≠ 不符合
            "condition_id": None,
            "item_type": "shop" if x.get("isShoppingItem") else "user",
            "category_id": _int((x.get("category") or {}).get("id")) or None,
            "brand_name": "",
            "seller_id": str(seller.get("userId") or "")[:32],
            "thumb_url": (x.get("imageUrl") or "")[:255],
            "listed_at": None,
            "updated_at_src": end,
            "end_time": end,
            # 0 是「拍卖但还没人出价」，None 是「读不到」—— 两者不能混
            "bid_count": _int(bids) if bids is not None else None,
            "buy_now_price": _int(x.get("buyNowPrice")) or None,
        }

    def _parse(self, blk: str) -> dict:
        buynow = _int(_attr(blk, "buynowprice"))
        item_type = "shop" if _attr(blk, "isshoppingitem") else "user"
        # 【商家出品的 buynowprice 是税抜，而 price 是税込】实测库里 5 件商家品全部满足
        # buynow × 110 // 100 == 税込一口价，分毫不差（如 909,091 → 1,000,000）。
        # 不换算的话面板上的「一口价」比实际要付的少 9%，同一张表里两列还不是一个口径。
        # 10% 消費税是法定常量，不是可调阈值；切り捨て（整数除）是日本税额显示的惯例，
        # round 会多 1 日元。detail() 那边直接读平台给的 taxinPrice，口径一致。
        if item_type == "shop" and buynow:
            buynow = buynow * 110 // 100
        return {
            "source": self.key,
            "item_id": _attr(blk, "id")[:32],
            "name": _attr(blk, "title"),
            # 【用当前价，不用一口价】拍卖的当前价会涨，所以这个数只在"此刻"成立；
            # 面板上会同时显示出价数和剩余时间，让你自己判断它还会涨到哪儿。
            "price": _int(_attr(blk, "price")),
            "status": "on_sale",              # 搜索页返回的都是进行中的
            # ヤフオク 的搜索结果【不给品相】。None 会让品相白名单对本源整体失效
            # （见 core/matcher.py 里的说明），这是有意的：不知道 ≠ 不符合。
            "condition_id": None,
            "item_type": item_type,
            "category_id": _int(_attr(blk, "category")) or None,
            "brand_name": "",
            "seller_id": _attr(blk, "auc-seller-id")[:32],
            "thumb_url": _attr(blk, "img")[:255],
            "listed_at": None,                # 搜索结果里没有上架时间，只有结束时间
            "updated_at_src": self.ts(_attr(blk, "endtime")),
            "end_time": self.ts(_attr(blk, "endtime")),
            "bid_count": _bids(blk),
            "buy_now_price": buynow or None,
        }


def _listing(html: str) -> dict | None:
    """新版搜索页的结果容器 {items, totalResultsAvailable}；不是新版页面就返回 None。

    先走已知路径；找不到再按形状找 —— 对方把它挪个位置、换个外层键名，
    只要容器本身还是这个形状就照样能用，不至于一改版就全瞎。
    【按形状找时优先认 "listing" 这个键】同一页里还有 featuredListing、newArrival
    这类推荐模块，形状一样，但里面的商品不是这次搜索的结果。
    """
    m = _NEXT_DATA.search(html)
    if not m:
        return None
    try:
        blob = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    props = blob.get("props") or {}
    for root in (props.get("pageProps") or {}, props):
        lst = ((((root.get("initialState") or {}).get("search") or {})
                .get("items") or {}).get("listing"))
        if _is_result_box(lst):
            return lst
    found = list(_result_boxes(blob))
    if not found:
        return None
    named = [box for key, box in found if key == "listing"]
    if named:
        return named[0]
    return max((box for _, box in found), key=lambda b: _int(b.get("totalResultsAvailable")))


def _is_result_box(o) -> bool:
    if not isinstance(o, dict) or "totalResultsAvailable" not in o:
        return False
    items = o.get("items")
    return isinstance(items, list) and (
        not items or (isinstance(items[0], dict) and "auctionId" in items[0]))


def _result_boxes(o, key: str = ""):
    """递归找所有「搜索结果容器」形状的 dict，连同它在父级里的键名一起产出。"""
    if isinstance(o, dict):
        if _is_result_box(o):
            yield key, o
        for k, v in o.items():
            yield from _result_boxes(v, k)
    elif isinstance(o, list):
        for v in o:
            yield from _result_boxes(v, key)


def _blocks(html: str) -> list[str]:
    """按 `<li class="Product">` 切块。

    【不能对整页 findall data-auction-id】同一个商品块里这个属性会出现好几次
    （图片链接一次、标题链接一次…），实测一页 47 件商品能抠出 141 个 id。
    切块之后每块只取第一个匹配，就对得上了。
    """
    return html.split('<li class="Product">')[1:]


def _attr(blk: str, name: str) -> str:
    """取块里第一个 data-auction-<name>。

    只取第一个是关键：split 出来的最后一块会拖着页面剩余的所有 HTML
    （页脚、推荐商品…），取第一个才保证拿到的是本商品的属性。
    """
    m = re.search(rf'data-auction-{name}="([^"]*)"', blk)
    return m.group(1) if m else ""


def _int(v: str) -> int:
    try:
        return int(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0


def _bids(blk: str) -> int | None:
    """出价数。只能靠 Product__bid 这个 class（搜索页里它是稳定的，不是构建哈希）。"""
    i = blk.find("Product__bid")
    if i < 0:
        return None
    m = _BID.search(blk[i:i + 400])
    return _int(m.group(1)) if m else None


def _extract_desc(html: str) -> str | None:
    """从商品页 HTML 里抠描述。

    锚点是「商品説明」这四个字 —— 不用 CSS class，因为 ヤフオク 的 class 是构建哈希
    （`gv-Heading--kAIOU7uq68gIXh6WMpSy` 这种），发一版就全变。
    界面文案也可能改，所以抠不到就返回空串：描述只用来打警示标签，
    没有它最多是少个黄标，绝不能因此把商品判成有问题或者不存在。
    """
    i = html.find("商品説明")
    if i < 0:
        # 【返回 None 而不是 ""】None＝没读到（锚点没了，多半是改版），""＝读到了但是空的。
        # 混为一谈的话解析一失效，每件商品都会被永久记成"描述已查、干净"。
        return None
    seg = html[i + len("商品説明"):i + 30000]
    j = seg.find("<h2")                       # 描述块结束，下一个小标题开始
    if j > 0:
        seg = seg[:j]
    seg = re.sub(r"<script.*?</script>", " ", seg, flags=re.S)
    seg = re.sub(r"<style.*?</style>", " ", seg, flags=re.S)
    seg = re.sub(r"<[^>]+>", " ", seg)
    return re.sub(r"\s+", " ", seg).strip()[:20000]
