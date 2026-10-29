"""捡漏标记（poller.mark_deals）：不合适了要摘掉、值没变不写库。

【起因】原先只给 matched = 1 的在售商品重算，一件捡漏涨价超过上限（price_over）
或卖家被拉黑（seller）之后，is_deal 永远停在 1、deal_pct 停在旧价。实测库里 8 行，
追踪页上一件 ¥129 万（上限 ¥120 万）的照样挂着绿色「捡漏」、写着「市价的 103%」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import poller  # noqa: E402

RULE = {"id": 1, "deal_ratio": 90, "deal_price": 1_150_000}


class FakeStore:
    def __init__(self, rows, median=1_078_000):
        self.rows, self.median, self.writes = rows, median, []

    def get_state(self, rid):
        return {"median_price": self.median}

    def query(self, sql, args=()):
        assert "status = 'on_sale'" in sql and "matched = 1" not in sql, \
            "不合适的在售商品也得选出来，否则它们的旧捡漏标没人摘"
        return [dict(r) for r in self.rows]

    def set_deal(self, source, item_id, rule_id, deal, pct):
        self.writes.append((item_id, deal, pct))


def _row(item_id, price, matched, is_deal, deal_pct):
    return {"source": "yahoo_flea", "item_id": item_id, "price": price,
            "matched": matched, "is_deal": is_deal, "deal_pct": deal_pct}


def test_涨价超上限之后捡漏标被摘掉_百分比跟着新价走(monkeypatch):
    fake = FakeStore([_row("z707838498", 1_290_000, 0, 1, 103)])
    monkeypatch.setattr(poller, "store", fake)
    poller.mark_deals(RULE)
    assert fake.writes == [("z707838498", 0, 119)]


def test_被拉黑的低价商品也不算捡漏(monkeypatch):
    fake = FakeStore([_row("m1", 500_000, 0, 1, 46)])
    monkeypatch.setattr(poller, "store", fake)
    poller.mark_deals(RULE)
    assert fake.writes == [("m1", 0, 46)]


def test_命中的照常判捡漏(monkeypatch):
    fake = FakeStore([_row("m2", 1_000_000, 1, 0, None)])
    monkeypatch.setattr(poller, "store", fake)
    poller.mark_deals(RULE)
    assert fake.writes == [("m2", 1, 92)]


def test_值没变就不写库_不合适的只在要摘捡漏标时才写(monkeypatch):
    """一条规则在售一两百件，每轮逐行 UPDATE 就是一两百个来回，而真变的通常是 0 件。
    不合适的那些 deal_pct 别处不看，中位数一变就把它们全重写一遍不值当。"""
    fake = FakeStore([_row("m3", 1_000_000, 1, 1, 92), _row("m4", 1_300_000, 0, 0, 120),
                      _row("m5", 1_300_000, 0, 0, None)])
    monkeypatch.setattr(poller, "store", fake)
    poller.mark_deals(RULE)
    assert fake.writes == []
