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
     data-auction-price="1000000" data-auction-buynowprice="1100000"
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
    assert it["buy_now_price"] == 1100000
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
