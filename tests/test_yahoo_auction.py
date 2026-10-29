"""ヤфオク HTML 解析的回归测试。

【这套测试比别的更要紧】ヤフオク 是三个源里唯一没有 JSON 接口的，只能解析 HTML。
Yahoo 前端一发版，解析就可能悄悄开始返回空结果 —— 而"搜到 0 件"和"真的没货"
在日志里长得一模一样。下面的 fixture 保留了真实页面的【结构】，但其中的商品ID、卖家ID、时间戳
都换成了明显的假值 —— 真 ID 会让人以为能打开，而那些拍卖早就结束、页面已失效。
哪天这些用例挂了，就是该去看 Yahoo 改了什么的信号。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.yahoo_auction import (  # noqa: E402
    YahooAuction, _attr, _bids, _blocks, _extract_desc, _int,
)

# 真实结构：一个商品块里 data-auction-id 会出现【多次】（图片链接一次、标题链接一次）
SEARCH_HTML = '''
<ul class="Products__items">
<li class="Product"> <div class="Product__image">
  <a class="Product__imageLink" data-auction-id="m0000000001"
     data-auction-category="2084211540"
     data-auction-title="GeForce MSI RTX 5090 グラフィックボード Z09"
     data-auction-img="https://auc-pctr.c.yimg.jp/i/x.jpg"
     data-auction-price="19500" data-auction-buynowprice="0"
     data-auction-endtime="1800000000" data-auction-startprice="1"
     data-auction-isflea="" data-auction-isfreeshipping=""
     data-auction-isshoppingitem="" data-auction-auc-seller-id="seller_dummy_a"></a>
  </div>
  <div class="Product__detail">
    <a class="Product__titleLink" data-auction-id="m0000000001"
       data-auction-title="GeForce MSI RTX 5090 グラフィックボード Z09">タイトル</a>
    <span class="Product__bid">
                            20</span>
    <span class="Product__time">1日</span>
  </div>
</li>
<li class="Product"> <div class="Product__image">
  <a class="Product__imageLink" data-auction-id="u0000000002"
     data-auction-category="2084211540"
     data-auction-title="ASUS ROG ASTRAL RTX 5090 OC"
     data-auction-img="https://auc-pctr.c.yimg.jp/i/y.jpg"
     data-auction-price="1000000" data-auction-buynowprice="1000000"
     data-auction-endtime="1800086400" data-auction-startprice="1000000"
     data-auction-isshoppingitem="1" data-auction-auc-seller-id="seller_dummy_b"></a>
  </div>
</li>
<li class="Product"> <div class="Product__image">
  <a class="Product__imageLink" data-auction-id="z0000000003"
     data-auction-category="2084211540"
     data-auction-title="新品未開封 GeForce RTX 5090 32G"
     data-auction-img="https://auc-pctr.c.yimg.jp/i/z.jpg"
     data-auction-price="930000" data-auction-buynowprice="0"
     data-auction-endtime="" data-auction-startprice="930000"
     data-auction-isflea="1" data-auction-isfreeshipping=""
     data-auction-isshoppingitem="" data-auction-auc-seller-id=""></a>
  </div>
</li>
</ul>
<div class="footer">全 47件</div>
'''


def test_按商品块切分_不会把同一件数成多件():
    # 【这是最容易踩的坑】整页 findall data-auction-id 会把一件商品数成两三件：
    # 实测一页 47 件商品能抠出 141 个 id
    assert len(_blocks(SEARCH_HTML)) == 3
    assert SEARCH_HTML.count('data-auction-id=') == 4      # 确认 fixture 里确实有重复


def test_フリマ商品被滤掉_不和独立的フリマ源重复():
    """搜索结果里 isflea=1 的是 Yahoo!フリマ 的货，我们有独立的源在抓它们。

    不滤掉＝同一件商品进两个源：面板上出现两遍，成交样本记两次把中位数带偏。
    fixture 里第三件就是 isflea=1，必须被挡在外面。
    """
    kept = [b for b in _blocks(SEARCH_HTML) if not _attr(b, "isflea")]
    assert [_attr(b, "id") for b in kept] == ["m0000000001", "u0000000002"]
    assert _attr(_blocks(SEARCH_HTML)[2], "isflea") == "1"   # 确认 fixture 里真有一件


def test_翻页偏移按未过滤的块数推进():
    """b= 是"从第几件开始"的偏移量，按过滤后的件数推进会退回去重抓。

    3 个块里滤掉 1 个 フリマ，下一页仍必须从 +3 处开始，不是 +2。
    """
    blocks = _blocks(SEARCH_HTML)
    kept = [b for b in blocks if not _attr(b, "isflea")]
    assert len(blocks) == 3 and len(kept) == 2
    assert 1 + len(blocks) == 4      # 正确：下一页从第 4 件开始
    assert 1 + len(kept) == 3        # 错误的写法会退回去重抓第 3 件


def test_取属性只取块里第一个():
    # split 出来的最后一块会拖着页脚等剩余 HTML，只取第一个才保证是本商品的
    blk = _blocks(SEARCH_HTML)[0]
    assert _attr(blk, "id") == "m0000000001"
    assert _attr(blk, "price") == "19500"


def test_出价数解析_能跨过中间的空白节点():
    # 真实 HTML 里 Product__bid 和数字之间隔着换行和缩进
    assert _bids(_blocks(SEARCH_HTML)[0]) == 20


def test_没有出价节点时返回None而不是0():
    # None = "这不是拍卖"，0 = "是拍卖但没人出价"。两者语义不同，面板上显示也不同
    assert _bids(_blocks(SEARCH_HTML)[1]) is None


def test_解析出完整的拍卖字段():
    src = YahooAuction()
    it = src._parse(_blocks(SEARCH_HTML)[0])
    assert it["source"] == "yahoo_auction"
    assert it["item_id"] == "m0000000001"
    assert it["price"] == 19500                 # 当前价，不是起拍价也不是一口价
    assert it["buy_now_price"] is None          # buynowprice=0 → 没有一口价
    assert it["bid_count"] == 20
    assert it["status"] == "on_sale"
    assert it["condition_id"] is None           # ヤフオク 搜索结果不给品相
    assert it["item_type"] == "user"
    assert it["end_time"] is not None
    assert it["listed_at"] is None              # 搜索结果只有结束时间，没有上架时间


def test_一口价和商家标记():
    src = YahooAuction()
    it = src._parse(_blocks(SEARCH_HTML)[1])
    assert it["buy_now_price"] == 1100000       # 商家品：搜索页给的 1,000,000 是税抜 → 税込 1,100,000
    assert it["bid_count"] is None
    assert it["item_type"] == "shop"            # isshoppingitem 非空


def test_千分位和空值不会让解析炸掉():
    assert _int("1,100,000") == 1100000
    assert _int("") == 0
    assert _int(None) == 0


# ---------------------------------------------------------------- 描述提取

DETAIL_HTML = '''
<div class="gv-Box--aBcDeF12">
  <h2 class="gv-Heading--kAIOU7uq68gIXh6WMpSy">商品説明</h2>
  <div class="sc-0-0 izpaCn">当商品は中古市場にて購入した<b>ジャンク品</b>となります。
     <br>【状態について】分解履歴・分解痕跡が確認できます。</div>
  <h2 class="gv-Heading--kAIOU7uq68gIXh6WMpSy">支払い方法</h2>
  <div>PayPay / クレジットカード</div>
</div>
'''


def test_描述以商品説明为锚点提取_不依赖CSS_class():
    # ヤフオク 的 class 是构建哈希（gv-Heading--kAIOU7uq68gIXh6WMpSy），
    # 前端发一版就全变；「商品説明」是界面文案，相对可靠
    d = _extract_desc(DETAIL_HTML)
    assert "ジャンク品" in d
    assert "分解履歴" in d


def test_描述在下一个小标题处截断():
    # 不截断的话会把「支払い方法」等后续内容也吞进来，警示词命中全是噪音
    assert "支払い方法" not in _extract_desc(DETAIL_HTML)
    assert "PayPay" not in _extract_desc(DETAIL_HTML)


def test_找不到锚点返回None_而不是空串():
    # 【None 和 "" 必须分开】None＝我们没读到（锚点没了，多半是 Yahoo 改版），
    # ""＝读到了、确实是空的。混为一谈的话解析一失效，每件商品都会被
    # 永久记成「描述已查、干净」，警示层对整个源静默失效而没人会发现。
    assert _extract_desc("<html><body>完全不同的页面</body></html>") is None
    assert _extract_desc("") is None


def test_有锚点但内容为空时返回空串():
    # 这种是"读到了，商品真的没写描述"，和读不到是两回事
    assert _extract_desc('<h2>商品説明</h2><div>  </div><h2>支払い方法</h2>') == ""


def test_描述读取失败不会被当成商品消失():
    # detail() 必须返回 dict（带 description=None），而不是 None ——
    # 返回 None 是"商品没了"的语义，会让上层把一件还在卖的商品标成已下架
    src = YahooAuction()
    src._call = lambda *a, **k: type("R", (), {
        "text": "<html>改版后的页面，没有任何已知锚点</html>", "status_code": 200})()
    d = src.detail("m123")
    assert d is not None
    assert d["description"] is None
    assert d["status"] == ""            # 状态也读不出 → 空串，让上层保持原状


# ---------------------------------------------------------------- 分页

def test_本页装不满就是最后一页():
    # 翻页终止【不靠】页面上那个「N件」——任何一处「◯◯件」都可能先被正则匹配上，
    # 抠错就会漏页或死循环。装不满＝到底了，这个判断不会错。
    src = YahooAuction()
    src._call = lambda *a, **k: type("R", (), {"text": SEARCH_HTML, "status_code": 200})()
    r = src.search("RTX 5090")
    # fixture 是 3 个块，其中 1 件 isflea=1 被滤掉 —— 这个 2 是"3 减 1"，
    # 不是"fixture 里有 2 件"。终止判断用的是过滤【前】的 3 块。
    assert len(r["items"]) == 2
    assert r["next"] == ""              # 3 块 < PAGE_SIZE，到底了


def test_成交检索直接返回空_不发请求():
    # 落札相場页只有构建哈希 class，没有可靠锚点，刻意不做。
    # 【必须不发请求】否则每天的成交轮都会白白打一次 Yahoo
    src = YahooAuction()
    called = []
    src._call = lambda *a, **k: called.append(1)
    r = src.search("RTX 5090", sold=True)
    assert r == {"items": [], "next": "", "total": 0}
    assert not called


def test_详情价必须取税込_不是税抜():
    """【这条是踩出来的】ヤフオク 详情 JSON 里 price 和 taxinPrice 并存：
      price      = 318182  税抜
      taxinPrice = 350000  税込（taxRate=10）
    而搜索页的 data-auction-price 给的是税込。取错的话同一件商品从搜索切到详情
    就凭空掉 9% —— 追踪时误报「已降」、污染价格历史，更要命的是 reconcile_sold
    拿它存成交样本，ヤフオク 的每条成交价都低 9%，把市价中位数整体往下拽。
    """
    import inspect

    from sources import yahoo_auction

    src = inspect.getsource(yahoo_auction.YahooAuction.detail)
    assert 'item.get("taxinPrice")' in src, "detail() 必须优先取税込价"
    # 兜底顺序：taxinPrice 优先，没有才退回 price
    assert src.index('taxinPrice') < src.index('or item.get("price")')


def test_商家一口价换算成税込_个人出品的不动():
    """实测 5 件商家品全部 buynow × 110 // 100 == 税込一口价（909,091 → 1,000,000）。
    个人出品的 buynowprice 本来就是税込，不许动。round 会多 1 日元，要整数除。"""
    from sources.yahoo_auction import YahooAuction
    src = YahooAuction.__new__(YahooAuction)
    shop = ('<a data-auction-id="s1" data-auction-title="t" data-auction-price="938300" '
            'data-auction-buynowprice="909091" data-auction-isshoppingitem="1" '
            'data-auction-endtime="0" data-auction-category="0" data-auction-img="" '
            'data-auction-auc-seller-id="x"></a>')
    user = shop.replace('data-auction-id="s1"', 'data-auction-id="u1"').replace('isshoppingitem="1"', 'isshoppingitem=""')
    assert src._parse(shop)["buy_now_price"] == 1_000_000
    assert src._parse(user)["buy_now_price"] == 909_091


# ---------------------------------------------------------------- 新版搜索页（2026-10 起）
# 2026-10 Yahoo 把搜索页换成了 Next.js：一个 <li class="Product"> 都没有了，结果全在
# __NEXT_DATA__ 里。旧解析器在新页面上一件都认不出来，却照常返回「0 件、已到底」——
# 上层把库里所有在售商品当成失踪，每轮拉详情核实，9 小时白花 1595 个请求。
# 下面的 fixture 保留新版 JSON 的【结构和字段名】，商品 ID / 卖家 ID 都是假的。
import json  # noqa: E402

import pytest  # noqa: E402

from sources.base import SourceBroken  # noqa: E402
from sources.yahoo_auction import PAGE_SIZE, _listing  # noqa: E402


def _auc(iid, *, title="ZOTAC GeForce RTX 4090", price=300000, bids=3, flea=False,
         shop=False, buynow=None, seller="FakeSellerUserId000000000001"):
    return {"auctionId": iid, "title": title, "price": price, "bidCount": bids,
            "endTime": "2026-10-18T19:45:14+09:00", "buyNowPrice": buynow,
            "buyNowPriceNoTax": (buynow * 100 // 110) if (buynow and shop) else buynow,
            "isShoppingItem": shop, "isFleamarketItem": flea,
            "category": {"id": 2084019008, "name": "PCIエクスプレス"},
            "seller": {"displayName": "********", "userId": seller},
            "imageUrl": "https://example.invalid/img.jpg"}


def _next_html(items, total=None, *, where=("props", "pageProps", "initialState", "search", "items"),
               box_key="listing", featured=None):
    box = {"isFetching": False, "items": items,
           "totalResultsAvailable": len(items) if total is None else total}
    node = {box_key: box,
            "featuredListing": {"items": featured or [], "totalResultsAvailable": len(featured or [])}}
    for k in reversed(where):
        node = {k: node}
    return ('<html><head><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(node, ensure_ascii=False) + "</script></head><body>"
            + "</body></html>")


def _src_with(html):
    src = YahooAuction()
    src._call = lambda *a, **k: type("R", (), {"text": html, "status_code": 200})()
    return src


def test_新版_解析出完整的拍卖字段():
    r = _src_with(_next_html([_auc("x0000000001")])).search("RTX 4090")
    assert len(r["items"]) == 1
    it = r["items"][0]
    assert it["source"] == "yahoo_auction" and it["item_id"] == "x0000000001"
    assert it["name"] == "ZOTAC GeForce RTX 4090" and it["price"] == 300000
    assert it["status"] == "on_sale" and it["bid_count"] == 3
    assert it["seller_id"] == "FakeSellerUserId000000000001", "和旧版 auc-seller-id 同值，拉黑列表靠它"
    assert it["item_type"] == "user" and it["buy_now_price"] is None
    assert it["category_id"] == 2084019008 and it["condition_id"] is None
    # 带 +09:00 的 ISO → 库里统一的 naive JST
    assert str(it["end_time"]) == "2026-10-18 19:45:14" and it["end_time"].tzinfo is None
    assert r["total"] == 1 and r["next"] == ""


def test_新版_商家一口价已是税込_不再乘1点1():
    # 旧版块上商家品的 buynowprice 是税抜，要 ×110//100；新版 buyNowPrice 已经是税込
    r = _src_with(_next_html([_auc("s0000000002", shop=True, buynow=37800)])).search("x")
    it = r["items"][0]
    assert it["item_type"] == "shop" and it["buy_now_price"] == 37800


def test_新版_没人出价是0不是None():
    it = _src_with(_next_html([_auc("x0000000003", bids=0)])).search("x")["items"][0]
    assert it["bid_count"] == 0


def test_新版_フリマ商品被滤掉_翻页按未过滤条数推进():
    raw = [_auc(f"x{i:010d}", flea=(i % 3 == 0)) for i in range(PAGE_SIZE)]
    r = _src_with(_next_html(raw, total=250)).search("x", page_token="1")
    assert len(r["items"]) == PAGE_SIZE - len([i for i in range(PAGE_SIZE) if i % 3 == 0])
    assert all(not it["item_id"] == f"x{0:010d}" for it in r["items"]), "isFleamarketItem 的要滤掉"
    assert r["next"] == str(1 + PAGE_SIZE), "偏移量按过滤前的条数推进"


def test_新版_推荐模块里的商品不算搜索结果():
    html = _next_html([_auc("x0000000004")], featured=[_auc("f0000000005")])
    ids = [it["item_id"] for it in _src_with(html).search("x")["items"]]
    assert ids == ["x0000000004"]


def test_新版_结果容器挪了位置也能按形状找到():
    html = _next_html([_auc("x0000000006")], where=("props", "pageProps", "newLayout", "results"))
    assert [it["item_id"] for it in _src_with(html).search("x")["items"]] == ["x0000000006"]
    # 形状一样的容器有好几个时，认 listing 这个键，不认推荐模块
    lst = _listing(_next_html([_auc("x0000000007")], where=("props", "a"),
                              featured=[_auc("f1"), _auc("f2")]))
    assert [x["auctionId"] for x in lst["items"]] == ["x0000000007"]


def test_页面说有货却一件ID都拿不到_抛SourceBroken而不是返回空():
    # 新版：容器在、总数 84、items 却是空的
    with pytest.raises(SourceBroken):
        _src_with(_next_html([], total=84)).search("x")
    # 再改一次版：既没有 __NEXT_DATA__ 也没有 Product 块，只剩页面上的「84件」
    with pytest.raises(SourceBroken):
        _src_with("<html><div>検索結果 84件</div></html>").search("x")


def test_真没货时正常返回空_不报错_并标明是平台说的():
    r = _src_with(_next_html([], total=0)).search("x")
    assert r["items"] == [] and r["total"] == 0 and r["next"] == ""
    assert r["empty_ok"] is True, "认出了结果容器、平台明确说 0 件 —— 轮询层据此放行，不当成解析失效"


def test_返回里带着过滤前的条数():
    r = _src_with(_next_html([_auc("x1"), _auc("z1", flea=True)])).search("x")
    assert len(r["items"]) == 1 and r["raw"] == 2 and r["empty_ok"] is False


def test_认不出主列表时宁可报改版_不拿推荐模块顶替():
    # featuredListing（注目のオークション）形状和主列表一样、但只有 1〜3 件。拿它顶替会让
    # 「能发现的改版」变成「悄悄只拿到 1 件」：两层 SourceBroken 都拦不住，对账照样空转。
    html = _next_html([_auc("f0000000001")], box_key="somethingElse",
                      featured=[_auc("f0000000001")]) + "<div>検索結果 84件</div>"
    with pytest.raises(SourceBroken):
        _src_with(html).search("x")


def test_列表头上插了个广告位_主列表照样认得出():
    html = _next_html([{"type": "AD"}, _auc("x0000000008")])
    assert [it["item_id"] for it in _src_with(html).search("x")["items"]] == ["x0000000008"]


def test_总数正好整百时多翻的那一页是空的_不算改版():
    r = _src_with(_next_html([], total=PAGE_SIZE)).search("x", page_token=str(PAGE_SIZE + 1))
    assert r["items"] == [] and r["next"] == ""


def test_一页全是フリマ商品_过滤后是0件_不算改版():
    r = _src_with(_next_html([_auc(f"z{i}", flea=True) for i in range(5)])).search("x")
    assert r["items"] == [] and r["total"] == 5


# ---------------------------------------------------------------- 新版详情页的描述

def _next_page(item: dict, extra_html: str = "") -> str:
    blob = {"props": {"pageProps": {"initialState": {"item": {"detail": {"item": item}}}}}}
    return (f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(blob, ensure_ascii=False)}'
            f"</script>{extra_html}")


def test_新版详情页取_descriptionHtml_全文_不切页面上的预览():
    """2026-10 实测：按「商品説明」切 HTML 切到的是 220 字的预览（以「...」结尾），
    ストア出品切到的是页面样板 —— 一件整机因样板里的「ジャンク品返品不可」被标了ジャンク。"""
    preview = "<h2>商品説明</h2><div>商品名：RTX 5090 ...</div><h2>支払い</h2>"
    item = {"status": "open", "bids": 0, "taxinPrice": 990000,
            "endTime": "2026-11-01T22:37:36+09:00",
            "descriptionHtml": "<CENTER><B>RTX 5090</B><BR>マイニング使用なし&amp;動作良好</CENTER>"}
    src = YahooAuction()
    src._call = lambda *a, **k: type("R", (), {"text": _next_page(item, preview), "status_code": 200})()
    d = src.detail("s1250109810")
    assert d["description"] == "RTX 5090 マイニング使用なし&動作良好"
    assert str(d["end_time"]) == "2026-11-01 22:37:36", "自動延長会推后截止，追踪刷新要能写回"


def test_新版详情页没给描述字段时记成没读到_不拿预览冒充():
    item = {"status": "open", "bids": 0, "taxinPrice": 525250}
    page = _next_page(item, "<p>…商品説明を確認するか、ストアに確認してください その他の情報 ジャンク品返品不可</p>")
    src = YahooAuction()
    src._call = lambda *a, **k: type("R", (), {"text": page, "status_code": 200})()
    assert src.detail("j1249948055")["description"] is None


def test_新版页面改了结构_也不退回去切页面上的预览():
    """有 __NEXT_DATA__、但路径变了：那是新版页面改了结构，不是旧版页面。退回切 HTML
    切到的是预览或样板，会被当成真描述永久存下（desc_checked=1 之后不再重拉）。"""
    blob = {"props": {"pageProps": {"initialState": {"itemV2": {"detail": {}}}}}}
    page = (f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(blob)}</script>'
            "<h2>商品説明</h2><div>商品名：RTX 5090 ...</div><h2>支払い</h2>")
    src = YahooAuction()
    src._call = lambda *a, **k: type("R", (), {"text": page, "status_code": 200})()
    assert src.detail("s1250109810")["description"] is None
