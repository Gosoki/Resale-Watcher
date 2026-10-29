"""Yahoo!フリマ 的搜索结果里混着 ヤフオク 的拍卖，源这一层要滤掉。

【起因】两边 ID 一样，库里成了同一件商品的两行：卖家 ID 两边不同（拉黑失效）、
这一行没有出价数（起拍价被当一口价算捡漏）、两边各拉一遍详情、3 件被推了两遍。
实测「RTX 4090」第一页 100 件里 41 件是 ヤフオク 的。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import yahoo_flea  # noqa: E402


def _raw(item_id, price=480000):
    return {"id": item_id, "title": f"RTX 4090 {item_id}", "price": price, "itemStatus": "OPEN",
            "seller": {"id": "p3260"}, "openTime": "2026-10-27T21:47:34+09:00",
            "endTime": "2026-10-29T21:47:34+09:00"}


def _src(payload):
    class Resp:
        def json(self):
            return payload

    src = yahoo_flea.YahooFlea.__new__(yahoo_flea.YahooFlea)
    src._call = lambda *a, **k: Resp()
    return src


def test_滤掉_ヤフオク_的商品():
    page = _src({"totalResultsAvailable": 3,
                 "items": [_raw("z708062698"), _raw("j1223120996"), _raw("1241582519")]}).search("RTX 4090")
    assert [x["item_id"] for x in page["items"]] == ["z708062698"]
    assert page["raw"] == 3, "过滤前的条数要报出去，不然一页全是 ヤフオク 时会被当成解析坏了"


def test_认不出的_ID_格式放行_不能让整个源静默失明():
    """只收 z 开头的话，哪天平台换了 ID 格式这个源就一件都收不到，而 raw > 0、「解析坏了」不响。"""
    page = _src({"totalResultsAvailable": 2,
                 "items": [_raw("y12345678"), _raw("ZX-0001")]}).search("RTX 4090")
    assert [x["item_id"] for x in page["items"]] == ["y12345678", "ZX-0001"]


def test_翻页偏移按过滤前的条数算():
    """offset 是平台的游标。按过滤后的 59 件算的话，下一页从第 60 件开始，重翻 41 件。"""
    raw = [_raw(f"z7{i:08d}") for i in range(59)] + [_raw(f"j12{i:08d}") for i in range(41)]
    page = _src({"totalResultsAvailable": 123, "items": raw}).search("RTX 4090")
    assert len(page["items"]) == 59 and page["next"] == "100"


def test_一页全是ヤフオク时_不翻到底也不算空():
    page = _src({"totalResultsAvailable": 150,
                 "items": [_raw(f"c12{i:08d}") for i in range(100)]}).search("RTX 4090")
    assert page["items"] == [] and page["raw"] == 100 and page["next"] == "100"


def test_ヤフオク的商品不拿フリマ的详情页去核实_对账直接收尾():
    src = yahoo_flea.YahooFlea.__new__(yahoo_flea.YahooFlea)
    assert src.can_detail("z708062698") and not src.foreign("z708062698")
    for iid in ("c1244386335", "1241582519"):
        assert not src.can_detail(iid) and src.foreign(iid)


def test_已售出页的_schema_块各种合法写法都不抛():
    """一抛就把这个源这一轮的对账整个打断。数组形的块、带小数的价格都是合法的 schema.org。"""
    arr = '<script type="application/ld+json">[{"@type":"BreadcrumbList"}]</script>'
    assert yahoo_flea._from_ld_json(arr) is None
    dec = ('<script type="application/ld+json">{"@type":"Product","name":"x",'
           '"offers":{"price":"1280000.00","availability":"https://schema.org/SoldOut"}}</script>')
    d = yahoo_flea._from_ld_json(dec)
    assert d["price"] == 1280000 and d["status"] == "sold_out"
    assert d["description"] is None, "块里没给描述＝没读到，不能当成「描述已查、干净」"
