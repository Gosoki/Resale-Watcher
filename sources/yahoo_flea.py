"""Yahoo!フリマ（旧 PayPayフリマ）。

搜索是一个【裸 GET 的 JSON 接口】，不用签名、不用登录，比 Mercari 还省事：
  GET /api/v1/search?query=…&results=100&offset=0&itemStatus=open|sold

详情没有对应的 API（/api/v1/items/{id} 一律 404），描述得从商品网页的
`__NEXT_DATA__` 里挖 —— 页面 ~380KB，比 Mercari 的 JSON 重，所以只对
初筛命中的少数候选拉，和 Mercari 那边的策略一样。

【和メルカリ是两个独立商品池】实测同一关键词两边重叠只有个位数，必须都搜。
"""
import json
import re

from sources.base import Source

SEARCH_URL = "https://paypayfleamarket.yahoo.co.jp/api/v1/search"
ITEM_PAGE = "https://paypayfleamarket.yahoo.co.jp/item/{}"
PAGE_SIZE = 100                       # 实测 results=100 有效，一次拿满

# Yahoo 的品相是字符串，Mercari 是 1~6 的数字。统一到 Mercari 那套，
# 这样规则表里的 condition_ids 白名单能跨平台通用，你只填一次。
# 实测取值只有 new/used10/used20/used40/used60（跳号，没有 30/50）。
CONDITION = {"new": 1, "used10": 2, "used20": 3, "used40": 4, "used60": 5, "used80": 6}

_NEXT_DATA = re.compile(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class YahooFlea(Source):
    key = "yahoo_flea"
    name = "Yahoo!フリマ"

    def item_url(self, item_id: str) -> str:
        return ITEM_PAGE.format(item_id)

    def search(self, keyword: str, *, sold: bool = False, page_token: str = "") -> dict:
        offset = int(page_token or 0)
        params = {"query": keyword, "results": PAGE_SIZE, "offset": offset,
                  "itemStatus": "sold" if sold else "open"}
        data = self._call("GET", SEARCH_URL, params=params).json()
        items = [self._parse(x) for x in (data.get("items") or [])]
        total = int(data.get("totalResultsAvailable") or 0)
        nxt = offset + len(items)
        return {"items": items, "next": str(nxt) if items and nxt < total else "", "total": total}

    def detail(self, item_id: str) -> dict | None:
        resp = self._call("GET", ITEM_PAGE.format(item_id))
        if resp.status_code == 404:
            return None
        m = _NEXT_DATA.search(resp.text)
        # 【description=None 和 "" 是两件事】None＝我们没读到（页面结构变了），
        # ""＝读到了、确实是空的。混为一谈的话，解析一旦失效，每件商品都会被
        # 当成"描述已查、干净"永久落库（desc_checked=1 之后再也不重试），
        # warn_desc 这一层对整个源静默失效，而面板上还写着「✓ 描述已查，干净」。
        # 返回整个 dict 而不是 None：None 是"商品没了"的语义，会让上层把它标成下架。
        if not m:
            return {"description": None, "price": 0, "name": "", "status": ""}
        try:
            blob = json.loads(m.group(1))
        except json.JSONDecodeError:
            return {"description": None, "price": 0, "name": "", "status": ""}
        item = _find_item(blob, item_id)
        if item is None:
            return {"description": None, "price": 0, "name": "", "status": ""}
        return {
            "description": item.get("description") or "",
            "price": int(item.get("price") or 0),
            "name": item.get("title") or "",
            "status": {"OPEN": "on_sale", "SOLD": "sold_out"}.get(item.get("itemStatus"), ""),
        }

    def _parse(self, raw: dict) -> dict:
        cat = raw.get("category") or {}
        brand = raw.get("brand") or {}
        return {
            "source": self.key,
            "item_id": str(raw.get("id") or "")[:32],
            "name": raw.get("title") or "",
            "price": int(raw.get("price") or 0),
            "status": {"OPEN": "on_sale", "SOLD": "sold_out"}.get(raw.get("itemStatus"), "on_sale"),
            "condition_id": CONDITION.get(raw.get("condition")),
            # フリマ 的搜索结果里没有商家/个人的区分标记，一律当个人出品
            "item_type": "user",
            "category_id": int(cat["id"]) if cat.get("id") else None,
            "brand_name": ((brand or {}).get("name") or "")[:64],
            "seller_id": str((raw.get("seller") or {}).get("id") or raw.get("sellerId") or "")[:24],
            "thumb_url": (raw.get("thumbnailImageUrl") or "")[:255],
            "listed_at": self.iso(raw.get("openTime")),
            # 没有单独的成交时间字段。endTime 是出品期限，售出时它就是这件商品
            # 生命周期的终点 —— 和 Mercari 那边用 updated 近似成交时间是同一个思路。
            "updated_at_src": self.iso(raw.get("endTime")),
            # フリマ 的 endTime 是出品期限（到期自动下架或续期），不是拍卖截止
            "end_time": self.iso(raw.get("endTime")),
            "bid_count": None, "buy_now_price": None,
        }


def _find_item(node, item_id: str, depth: int = 0):
    """在 __NEXT_DATA__ 这坨嵌套里找出目标商品。

    Next.js 的 props 结构会随前端改版变形，所以不写死路径，按【有 id 且等于目标】来认，
    并且要求它同时带 description —— 页面里还有「推荐商品」等同构对象，只认 id 会抓错。
    """
    if depth > 12:
        return None
    if isinstance(node, dict):
        if node.get("id") == item_id and "description" in node:
            return node
        for v in node.values():
            r = _find_item(v, item_id, depth + 1)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_item(v, item_id, depth + 1)
            if r:
                return r
    return None
