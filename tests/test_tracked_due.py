"""tracked_due 的 SQL：交易中的按 trading_min 算，其余按 track_min；LIVE 那个字面量不能丢。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402


def test_交易中的用更长的间隔_且仍只挑LIVE():
    got = {}
    saved = store.query
    store.query = lambda sql, params=None: got.update(sql=sql, params=params) or []
    try:
        store.tracked_due(5, 10, 180)
    finally:
        store.query = saved
    assert "CASE WHEN status = 'trading' THEN %s ELSE %s END" in got["sql"]
    assert "status IN ('on_sale', 'trading')" in got["sql"]
    t_trade, t_track, limit = got["params"]
    assert (t_track - t_trade).total_seconds() == (180 - 5) * 60, "交易中的截止要比在售的早 175 分钟"
    assert limit == 10


def test_没给trading_min时退回track_min():
    got = {}
    saved = store.query
    store.query = lambda sql, params=None: got.update(sql=sql, params=params) or []
    try:
        store.tracked_due(5, 10)
    finally:
        store.query = saved
    t_trade, t_track, _ = got["params"]
    assert t_trade == t_track
