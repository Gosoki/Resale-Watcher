"""Mercari（メルカリ）。

用的是网页版自己在调的那套接口，免登录：
  POST /v2/entities:search   搜索（带分页）
  GET  /items/get?id=...     详情（描述只有这里有）
两者都要一个 DPoP 头：用一对 ES256 密钥给「方法+URL」签个 JWT，公钥直接放在 JWT 头里。
"""
import base64
import json
import time
import uuid

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from sources.base import UA, Source

SEARCH_URL = "https://api.mercari.jp/v2/entities:search"
DETAIL_URL = "https://api.mercari.jp/items/get"

STATUS_MAP = {"ITEM_STATUS_ON_SALE": "on_sale",
              "ITEM_STATUS_TRADING": "trading",
              "ITEM_STATUS_SOLD_OUT": "sold_out"}


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _seller(raw) -> str:
    """卖家ID。"0" 是 メルカリShops 的哨兵值（店铺不是用户），当成未知。"""
    sid = str(raw or "").strip()
    return "" if sid == "0" else sid[:32]


class Mercari(Source):
    key = "mercari"
    name = "メルカリ"

    def __init__(self) -> None:
        super().__init__()
        # 密钥对进程内固定：网页版也是一个会话用一对，每次请求换新密钥反而不像正常客户端。
        self._pkey = ec.generate_private_key(ec.SECP256R1())
        n = self._pkey.public_key().public_numbers()
        self._jwk = {"crv": "P-256", "kty": "EC",
                     "x": _b64u(n.x.to_bytes(32, "big")), "y": _b64u(n.y.to_bytes(32, "big"))}

    def _headers(self, method: str, url: str) -> dict:
        hdr = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self._jwk}
        pl = {"iat": int(time.time()), "jti": str(uuid.uuid4()),
              "htu": url, "htm": method, "uuid": str(uuid.uuid4())}
        msg = (f"{_b64u(json.dumps(hdr, separators=(',', ':')).encode())}."
               f"{_b64u(json.dumps(pl, separators=(',', ':')).encode())}")
        r, s = decode_dss_signature(self._pkey.sign(msg.encode(), ec.ECDSA(hashes.SHA256())))
        dpop = f"{msg}.{_b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        return {"dpop": dpop, "x-platform": "web", "accept": "*/*",
                "content-type": "application/json", "user-agent": UA,
                "origin": "https://jp.mercari.com", "referer": "https://jp.mercari.com/"}

    def item_url(self, item_id: str) -> str:
        # Shops 的商品 ID 不是 mXXXX 格式，商品页路径也不一样
        return (f"https://jp.mercari.com/item/{item_id}" if item_id.startswith("m")
                else f"https://jp.mercari.com/shops/product/{item_id}")

    def search(self, keyword: str, *, sold: bool = False, page_token: str = "") -> dict:
        body = {
            "userId": "", "pageSize": 120, "pageToken": page_token,
            "searchSessionId": uuid.uuid4().hex,
            "indexRouting": "INDEX_ROUTING_UNSPECIFIED", "thumbnailTypes": [],
            "searchCondition": {
                "keyword": keyword, "excludeKeyword": "", "sort": "SORT_CREATED_TIME",
                "order": "ORDER_DESC",
                "status": ["STATUS_SOLD_OUT"] if sold else ["STATUS_ON_SALE"],
                "sizeId": [], "categoryId": [], "brandId": [], "sellerId": [],
                "priceMin": 0, "priceMax": 0,
                "itemConditionId": [], "shippingPayerId": [], "shippingFromArea": [],
                "shippingMethod": [], "colorId": [], "hasCoupon": False, "attributes": [],
                "itemTypes": [], "skuIds": [], "shopIds": [], "excludeShippingMethodIds": [],
            },
            "defaultDatasets": [], "serviceFrom": "suruga",
            "withItemBrand": True, "withItemSize": False, "withItemPromotions": False,
            "withItemSizes": False, "withShopname": False, "useDynamicAttribute": True,
            "withSuggestedItems": False, "withOfferPricePromotion": False,
            "withProductSuggest": False, "withParentProducts": False,
            "withProductArticles": False, "withSearchConditionId": False,
        }
        data = self._call("POST", SEARCH_URL, json_body=body).json()
        meta = data.get("meta") or {}
        return {
            "items": [self._parse(raw) for raw in (data.get("items") or [])],
            "next": meta.get("nextPageToken") or "",
            "total": int(meta.get("numFound") or 0),
        }

    def detail(self, item_id: str) -> dict | None:
        # メルカリShops 的商品 ID 不是 mXXXX 格式，/items/get 拿不到它们 ——
        # 不先挡掉的话，每个 Shops 商品每轮都要打一次注定失败的请求。
        # 【但绝不能返回 None】None 的语义是「查无此物」，上层会据此把商品标成
        # 已下架：开了 allow_shops 的规则，Shops 商品会命中一轮就从命中页消失。
        # 返回 description=None 表示「我们读不到」，status="" 表示「状态未知」——
        # 上层会保持原状，和详情页解析失败走同一条路。
        if not item_id.startswith("m"):
            return {"description": None, "price": 0, "name": "", "status": ""}
        resp = self._call("GET", DETAIL_URL,
                          params={"id": item_id, "country_code": "", "view": "1"})
        if resp.status_code == 404:
            return None
        d = (resp.json() or {}).get("data")
        if not d:
            return None
        return {
            "description": d.get("description") or "",
            "price": int(d.get("price") or 0),
            "name": d.get("name") or "",
            # Mercari 详情接口给的状态字符串和我们库里的取值恰好同名，不用映射
            "status": d.get("status") or "",
        }

    def _parse(self, raw: dict) -> dict:
        return {
            "source": self.key,
            "item_id": raw.get("id") or "",
            "name": raw.get("name") or "",
            "price": int(raw.get("price") or 0),
            "status": STATUS_MAP.get(raw.get("status"), "on_sale"),
            "condition_id": int(raw["itemConditionId"]) if raw.get("itemConditionId") else None,
            # ITEM_TYPE_BEYOND 就是メルカリShops 的商家出品
            "item_type": "user" if raw.get("itemType") == "ITEM_TYPE_MERCARI" else "shop",
            "category_id": int(raw["categoryId"]) if raw.get("categoryId") else None,
            "brand_name": ((raw.get("itemBrand") or {}).get("name") or "")[:64],
            # 【"0" 是哨兵不是卖家】メルカリShops 的商品卖家是店铺实体不是用户，
            # 接口对它们一律返回 sellerId="0"。实测库里 41 件商品顶着这个"卖家ID"，
            # 全是 Shops 品、分属不同店铺 —— 原样留着的话，卖家黑名单里填一个 0
            # 就会一次误杀这 41 件，而人以为自己只拉黑了一家店。归一成空串＝卖家未知。
            "seller_id": _seller(raw.get("sellerId")),
            "thumb_url": (raw.get("thumbnails") or [""])[0][:255],
            "listed_at": self.ts(raw.get("created")),
            "updated_at_src": self.ts(raw.get("updated")),
            # Mercari 是定价销售，没有拍卖那套
            "end_time": None, "bid_count": None, "buy_now_price": None,
        }
