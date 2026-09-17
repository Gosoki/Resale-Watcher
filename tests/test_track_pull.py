"""追踪页的「一键拉取」。

【为什么这几行值得单独测】refresh_tracked 平时受两道闸管着（track_min 控间隔、
track_budget 控每轮件数），手动拉取要的恰恰是把这两道都放开。放开得不干净的话
失败是静默的：你点了按钮、页面转了一下、什么都没变 —— 因为没到 track_min，
一件都没被选中。这种"点了没反应"在日志里也看不出异常，只能靠测试锁住。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sources as real_sources                                   # noqa: E402
from core import poller                                          # noqa: E402

SETTINGS = {"track_min": 5, "track_budget": 3}


def item(i):
    return {"source": "mercari", "item_id": f"m{i}", "rule_id": 1,
            "price": 700000, "name": f"商品{i}"}


class FakeStore:
    """只实现 refresh_tracked 真正用到的那几个方法，并记下 tracked_due 收到的参数。"""

    def refresh_median(self, rid):   # 成交样本进来后会刷中位数
        return (None, 0)

    TERMINAL = ("sold_out", "gone")

    def __init__(self, n):
        self.rows = [item(i) for i in range(n)]
        self.due_args = None

    def get_settings(self):
        return SETTINGS

    def tracked_items(self):
        return list(self.rows)

    def tracked_due(self, track_min, limit, trading_min=None):
        self.due_args = (track_min, limit)
        return self.rows[:limit]

    def touch_seen(self, *a):
        pass

    def update_tracked(self, *a):
        pass

    def set_status(self, *a):
        pass


class FakeSources:
    DailyLimitReached = real_sources.DailyLimitReached

    def get(self, name):
        return self

    def can_detail(self, iid):
        return True

    def detail(self, item_id):
        return {"status": "on_sale", "price": 700000, "name": "x",
                "description": "", "ship_from": "", "bid_count": None}


def run(n, **kw):
    fake, saved = FakeStore(n), (poller.store, poller.sources)
    poller.store, poller.sources = fake, FakeSources()
    try:
        return poller.refresh_tracked(**kw), fake
    finally:
        poller.store, poller.sources = saved


def test_一键拉取要把两道闸都放开():
    done, fake = run(10, force=True)
    assert fake.due_args == (0, 10), (
        "手动拉取必须传 track_min=0 且上限=追踪总件数。"
        f"实际传了 {fake.due_args} —— 会漏掉没到点的件")
    assert done == 10, f"10 件应该全拉，实际只拉了 {done}"


def test_常驻轮询照旧受两道闸管着():
    done, fake = run(10)
    assert fake.due_args == (SETTINGS["track_min"], SETTINGS["track_budget"])
    assert done == 3, "没加 force 时必须还按 track_budget 切，不然配额会被烧穿"


def test_一件都没追时不发请求():
    done, fake = run(0, force=True)
    assert done == 0 and fake.due_args == (0, 0)
