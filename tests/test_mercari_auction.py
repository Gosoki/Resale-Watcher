"""メルカリ 的拍卖商品。

【这一套守的是一个我真犯过的错】代码里曾经写死一句「メルカリ 是定价销售，没有拍卖那套」。
メルカリ 2025-01-29 就上线了オークション機能，那句话从此是错的 —— 而且错得很隐蔽：
搜索接口返回的每件商品都带一个 auction 字段，【不开 withAuction 开关时它恒为 null】，
看起来完全像"メルカリ 确实没有拍卖"。我照着这个现象查了 800 件、试了一圈枚举，
一度得出"接口不支持"的结论，直到用户指着一件真拍卖商品说"这不就是吗"。

下面的数据是 2026-09-14 从真实接口抓到的原样，不是编的。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sources  # noqa: E402
from sources.mercari import _bids  # noqa: E402

# 搜索接口的原样返回（withAuction=True 时）
SEARCH_AUCTION = {
    "id": "m72254949864", "name": "MSI GeForceRTX4090SUPRIM LIQUID X水冷24G",
    "price": 425200, "status": "ITEM_STATUS_ON_SALE", "itemType": "ITEM_TYPE_MERCARI",
    "thumbnails": ["x"], "created": "0", "updated": "0",
    "auction": {"id": "", "bidDeadline": "2026-09-14T11:33:00Z", "totalBid": "9",
                "highestBid": "425200", "initialPrice": "410000"},
}
SEARCH_NO_BID = dict(SEARCH_AUCTION, id="m36253442234",
                     auction=dict(SEARCH_AUCTION["auction"], totalBid="0"))
SEARCH_PLAIN = dict(SEARCH_AUCTION, id="m11111111111", auction=None)


def parse(raw):
    return sources.get("mercari")._parse(raw)


# ---------------------------------------------------------------- 出价数

def test_出价数从字符串转成整数():
    """【两个接口都把数字写成字符串】搜索给 "9"，直接当 int 用会在别处炸。"""
    assert parse(SEARCH_AUCTION)["bid_count"] == 9


def test_零次出价和不是拍卖必须分得开():
    """【这条最容易写错】拿不到就返回 0 的话，普通商品会被当成"拍卖但没人出价"，
    面板上每一件都挂出「🔨 拍卖 · 暂无人出价」。两者显示完全不同，不能混。"""
    assert parse(SEARCH_NO_BID)["bid_count"] == 0        # 是拍卖，还没人出价
    assert parse(SEARCH_PLAIN)["bid_count"] is None      # 根本不是拍卖
    assert _bids(None) is None and _bids("") is None
    assert _bids("0") == 0 and _bids(0) == 0


# ---------------------------------------------------------------- 结束时间

def test_结束时间按UTC解析再转JST():
    """【bidDeadline 是 UTC 的 Z 格式，不是 JST】当成本地时间读会差 9 小时 ——
    面板上「剩 3 小时」会显示成「剩 12 小时」，而拍卖恰恰是最后几分钟定胜负的。
    真实核对过：这条商品页面上写的就是「終了予定時刻 2026年9月14日 20:33」。"""
    end = parse(SEARCH_AUCTION)["end_time"]
    assert end.strftime("%Y-%m-%d %H:%M") == "2026-09-14 20:33"


def test_普通商品没有结束时间():
    assert parse(SEARCH_PLAIN)["end_time"] is None


# ---------------------------------------------------------------- 一口价

def test_メルカリ的拍卖没有一口价():
    """ヤフオク 有一口价，メルカリ 没有 —— 只能竞价。
    这里填上非 None 的话，面板会显示一个不存在的「一口价 ¥X」。"""
    assert parse(SEARCH_AUCTION)["buy_now_price"] is None


# ---------------------------------------------------------------- 开关本身

def test_搜索必须带上withAuction开关():
    """【少了它，上面所有解析都白写】不开这个开关时 auction 字段恒为 null，
    代码不会报错、不会进日志，只是所有拍卖商品都显示成普通定价商品。
    这正是这个 bug 藏了这么久的原因。"""
    import inspect
    assert '"withAuction": True' in inspect.getsource(sources.get("mercari").search)


def test_详情必须带上include_auction参数():
    """同理：详情接口不带这个参数，返回里根本没有 auction_info 这一节。
    追踪中的拍卖商品靠详情刷新出价数，少了它出价数永远停在第一次抓到的值。"""
    import inspect
    assert "include_auction" in inspect.getsource(sources.get("mercari").detail)
