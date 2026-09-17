"""2026-09-17 体检里证实的四个"不报错、只浪费"的口子。

  E  sync_schema 拿 schema.sql 整行去 MODIFY COLUMN：app_setting.k / poller_lease.id
     的定义里带 inline PRIMARY KEY，改一句注释就会在启动路径上报
     "Multiple primary key defined" —— 进程起不来。
  F  メルカリShops 的商品（ID 不是 m 开头）详情接口不支持，但在对账里照样占一份
     detail_budget、照样被 touch_seen 成"刚核实过" —— 永远确认不了下架、永远不会失联。
  G  追踪刷新接住 RateLimited 之后接着刷同源下一件：一件 60 秒退避刚睡完就再撞一次，
     track_budget=10 时最坏一轮睡 2520 秒。
  H  面板「一键抓取 / 立即跑一轮 / 一键拉取」和后台轮询零互斥：日志里 7 例同一
     规则×源在几十秒内被扫两遍，两边各拉一遍同一批详情。
"""
import sys
import threading
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import poller  # noqa: E402
from db import store  # noqa: E402
from sources import base, mercari  # noqa: E402


# ---------------------------------------------------------------- E

def test_MODIFY定义要剥掉inline_PRIMARY_KEY():
    want = {("app_setting", "k"): "k  VARCHAR(64) NOT NULL PRIMARY KEY COMMENT '新注释'",
            ("item", "price"): "price INT NOT NULL COMMENT '也改了'"}
    have = {("app_setting", "k"): {"comment": "旧注释", "type": "varchar(64)"},
            ("item", "price"): {"comment": "旧", "type": "int"}}
    adds, mods = store.plan_schema_changes(want, have)
    assert adds == []
    by_col = {c: d for _, c, d in mods}
    assert "PRIMARY KEY" not in by_col["k"], "MODIFY 里带 PRIMARY KEY 会报 Multiple primary key defined"
    assert "VARCHAR(64) NOT NULL COMMENT '新注释'" in by_col["k"], "只该剥主键，别的一个字都不能动"
    assert by_col["price"] == "price INT NOT NULL COMMENT '也改了'"


# ---------------------------------------------------------------- F

def test_默认所有商品都能拉详情_只有メルカリShops不能():
    class Any(base.Source):
        key, name = "x", "X"
        def search(self, *a, **k): ...
        def detail(self, *a, **k): ...
        def item_url(self, *a, **k): ...
    assert Any.__new__(Any).can_detail("whatever") is True
    m = mercari.Mercari.__new__(mercari.Mercari)
    assert m.can_detail("m12345678901") is True
    assert m.can_detail("2JVBYDjdtfKpHAHWwHtqoc") is False, "Shops 的 ID 不是 m 开头"


class ReconcileStore:
    def update_tracked(self, *a, **k):   # 对账核实到在售时会写回价/出价数
        pass

    def refresh_median(self, rid):   # 成交样本进来后会刷中位数
        return (None, 0)

    def get_settings(self, force=False):   # _search 读 search_reuse_min；空字典＝不复用
        return {}

    def __init__(self, items):
        self.items, self.touched, self.statuses = items, [], []
    def on_sale_items(self, rid, src): return self.items
    def touch_seen(self, *k): self.touched.append(k[1])
    def set_status(self, source, iid, rid, status, sold_at=None, price=None): self.statuses.append((iid, status))
    def add_sold_sample(self, *a): ...


class ReconcileSrc:
    key = "mercari"
    def __init__(self): self.asked = []
    def can_detail(self, iid): return iid.startswith("m")
    def detail(self, iid):
        self.asked.append(iid)
        return {"status": "on_sale", "price": 1, "description": ""}


def test_对账跳过Shops_不占预算不touch():
    old = config.now() - timedelta(hours=2)
    items = [{"item_id": "2JVshop", "last_seen_at": old, "matched": 1},
             {"item_id": "m1", "last_seen_at": old, "matched": 1},
             {"item_id": "m2", "last_seen_at": old, "matched": 1}]
    fake, src = ReconcileStore(items), ReconcileSrc()
    saved = poller.store
    poller.store = fake
    try:
        poller.reconcile_sold(src, {"id": 1, "name": "r", "missing_grace_min": 20,
                                    "detail_budget": 2}, seen=set())
    finally:
        poller.store = saved
    assert src.asked == ["m1", "m2"], "预算 2 应该全给能拉的两件，Shops 不占"
    assert "2JVshop" not in fake.touched, "Shops 没核实过，不许 touch 成刚见过"


# ---------------------------------------------------------------- G

def test_退避睡过的限流带backed_off标():
    """base.py 睡完退避抛出来的那个异常要带标；追踪刷新靠它区分"真限流"和"单件失败"。"""
    import inspect
    src = inspect.getsource(base.Source._call)
    i = src.index("time.sleep(back)")
    assert "backed_off = True" in src[i:i + 600]


class TrackStore:
    def refresh_median(self, rid):   # 成交样本进来后会刷中位数
        return (None, 0)

    def __init__(self, rows): self.rows, self.touched, self.samples = rows, [], []
    def add_sold_sample(self, rid, source, iid, price, sold_at, kind): self.samples.append((iid, price, kind))
    def get_settings(self, force=False): return {"track_min": 5, "track_budget": 10}
    def tracked_due(self, m, n, trading_min=None): return self.rows
    def tracked_items(self): return self.rows
    def touch_seen(self, *k): self.touched.append(k[1])
    def update_tracked(self, *a): ...
    def set_status(self, *a, **k): ...
    TERMINAL = ("sold_out", "gone")


def run_tracked(rows, behaviour):
    """behaviour[item_id] = 'ok' | 'limited' | 'single' | 'shop'"""
    asked = []

    class Src:
        key = "mercari"
        def can_detail(self, iid): return behaviour.get(iid) != "shop"
        def detail(self, iid):
            asked.append(iid)
            b = behaviour.get(iid, "ok")
            if b == "limited":
                e = base.RateLimited("HTTP 429"); e.backed_off = True; raise e
            if b == "single":
                raise base.RateLimited("HTTP 403（详情）")
            if b == "sold":
                return {"status": "sold_out", "price": 480_000, "bid_count": None}
            if b == "sold_noprice":
                return {"status": "sold_out", "price": 0, "bid_count": None}
            return {"status": "on_sale", "price": 1, "bid_count": None}

    saved_store, saved_get = poller.store, poller.sources.get
    fake = TrackStore(rows)
    poller.store = fake
    poller.sources.get = lambda key: Src()
    try:
        poller.refresh_tracked()
    finally:
        poller.store, poller.sources.get = saved_store, saved_get
    return asked, fake


def row(iid, matched=1):
    return {"source": "mercari", "item_id": iid, "rule_id": 1, "price": 1, "name": iid, "matched": matched}


def test_真限流后同源剩下的留到下一轮():
    asked, _ = run_tracked([row("m1"), row("m2"), row("m3")], {"m1": "limited"})
    assert asked == ["m1"], f"m1 睡完退避之后不该再刷同源的 m2/m3，实际刷了 {asked}"


def test_单件失败只跳那一件_不cool整个源():
    """一件长期坏掉的商品要是每轮把整个源 cool 掉，同源别的从此再也刷不到。"""
    asked, _ = run_tracked([row("m1"), row("m2")], {"m1": "single"})
    assert asked == ["m1", "m2"]


def test_追踪里的Shops一个请求都不发():
    asked, fake = run_tracked([row("2JVshop"), row("m1")], {"2JVshop": "shop"})
    assert asked == ["m1"]
    assert "2JVshop" not in fake.touched


# ---------------------------------------------------------------- H

def test_面板动作拿不到扫描闸就抛ScanBusy():
    """闷头阻塞的话面板只会静默卡住，人会以为坏了再点一次。"""
    saved = poller.MANUAL_WAIT_SEC
    poller.MANUAL_WAIT_SEC = 0.05
    poller.SCAN_LOCK.acquire()                      # 假装后台正在扫
    try:
        try:
            poller.manual(lambda: "ran")
        except poller.ScanBusy:
            pass
        else:
            raise AssertionError("后台持锁时手动动作应该抛 ScanBusy")
    finally:
        poller.SCAN_LOCK.release()
        poller.MANUAL_WAIT_SEC = saved
    assert poller.manual(lambda: "ran") == "ran", "锁空着就该直接跑"
    assert not poller.SCAN_LOCK.locked(), "跑完必须放锁"


def test_面板动作抛异常也要放锁():
    def boom(): raise RuntimeError("x")
    try:
        poller.manual(boom)
    except RuntimeError:
        pass
    assert not poller.SCAN_LOCK.locked()


def test_后台拿不到闸就跳过本轮而不是等():
    """等的话心跳会停，面板就要报「轮询已停止」，看门狗还会推告警。"""
    import inspect
    src = inspect.getsource(poller.loop)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    i = code.index("SCAN_LOCK.acquire(timeout=0)")
    assert "continue" in code[i:i + 300], "拿不到闸要 continue，不能阻塞"
    assert "SCAN_LOCK.release()" in code and "finally" in code


def test_追踪确认售出的进成交样本():
    """【最准的样本原先整批丢失】拉过详情、过了完整规则、价格是接口给的最终价 ——
    对账那边一直在记，追踪这边漏了。"""
    _, fake = run_tracked([row("m1")], {"m1": "sold"})
    assert fake.samples == [("m1", 480_000, "tracked")]


def test_追踪售出但没过规则的不进样本():
    _, fake = run_tracked([row("m1", matched=0)], {"m1": "sold"})
    assert fake.samples == []


def test_追踪售出但价格读不到的不进样本():
    _, fake = run_tracked([row("m1")], {"m1": "sold_noprice"})
    assert fake.samples == []
