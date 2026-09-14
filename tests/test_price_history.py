"""「上一次的价格」：从 price_log 里往回找第一个和现价不同的价。

【为什么不能直接取倒数第二条】price_log 不带 rule_id（价格是商品自身的属性），
而 upsert_item 是按规则调的 —— 同一个链接被两条规则命中就会各记一条。
add_price_log 现在会去重，但库里留着 27 条去重加上去【之前】写下的同价记录
（全在 2026-09-12 17:36〜18:53 那一段，涉及 26 件商品）。对这些商品，
倒数第二条就是现价本身，页面上会写成「上次 ¥2,650 · 现在 ¥2,650」——
不报错、不进日志，看着像程序坏了。

【为什么值得单独测】这个函数的输出直接印在页面上当决策依据。算错的两个方向
都不会报错：多退一格会把更早的价说成"上次"（你以为它跌了两万，其实只跌了四千），
少退一格就是上面那种自己等于自己。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.store import last_change  # noqa: E402

T0 = datetime(2026, 9, 12, 10, 0)


def log(*prices):
    """按时间升序的价格历史，每条隔一小时。"""
    return [{"price": p, "noted_at": T0 + timedelta(hours=i)}
            for i, p in enumerate(prices)]


def test_从没变过价的没有上次价格():
    assert last_change(log(500_000), 500_000) is None


def test_变过一次时上次价格就是原价():
    prev, at = last_change(log(500_000, 480_000), 480_000)
    assert prev == 500_000
    assert at == T0 + timedelta(hours=1), "变成现价的时间应该是新价那条的时间"


def test_变过多次时只回退一档():
    """【这是最容易写错的一处】「上一次」是紧挨着现在的那一档，不是最早那一档。
    退多了的话，一件 ¥34 万→¥33.6 万→¥32.5 万 的商品会显示成「上次 ¥340,000」，
    你会以为它刚跌了一万五，实际只跌了一万一 —— 而你是拿这个数去判断还会不会再跌的。
    """
    prev, _ = last_change(log(340_000, 336_000, 325_000), 325_000)
    assert prev == 336_000


def test_末尾连着的同价记录要跳过():
    """库里真实存在的那 26 件：同一个价被记了两遍。
    不跳过的话「上次」会等于「现在」。
    """
    assert last_change(log(2650, 2650), 2650) is None
    prev, at = last_change(log(3000, 2650, 2650), 2650)
    assert prev == 3000
    assert at == T0 + timedelta(hours=1), "时间要取第一次记到现价那条，不是重复的那条"


def test_中间的重复记录不影响回退一档():
    prev, _ = last_change(log(3000, 3000, 2800, 2650), 2650)
    assert prev == 2800


def test_价格涨回原值时不当成没变过():
    """【拍卖会这样】¥41 万起拍，一路被抬到 ¥46 万；中途也可能跌回某个旧价。
    只要现价和上一档不同，就该显示上一档 —— 哪怕这个价历史上出现过。
    """
    prev, _ = last_change(log(410_000, 425_000, 410_000), 410_000)
    assert prev == 425_000


def test_历史里根本没记过现价时仍给得出上次价格():
    """【防的是历史和 item 表对不上】真发生的话是别处有 bug，但这一行不该跟着崩，
    也不该沉默地少显示一档 —— 那反而会把真正的问题藏起来。
    """
    prev, at = last_change(log(500_000, 480_000), 470_000)
    assert prev == 480_000
    assert at is None, "没记过现价就说不出它是什么时候变的，只能不说"


def test_空历史不崩():
    assert last_change([], 500_000) is None
