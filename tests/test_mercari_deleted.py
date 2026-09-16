"""メルカリ 用 403 报 404：已删除的商品，详情接口回的是

    HTTP 403 {"result":"error","errors":[{"code":"InvisibleItemException",
              "message":"該当する商品は削除されています。"}],"meta":{"sub_error_code":"404"}}

（2026-09-17 拿库里一件卡了三天的 m32209355337 实测；对照一件交易中的回 200。）

【原先的后果】403 一律按限流处理：睡 60 秒（握着 メルカリ 的锁，所有规则一起等）、
计一次失败、抛 RateLimited 让这条规则这一轮作废。一件删掉的商品只要还标着在售，
就每轮都来一遍，永远不会被标成 gone；排在它后面的失踪商品也永远轮不到核实。
实测 2026-09-14 一整天 メルカリ 197 次「限流」、09-16 726 次，全是这个 ——
用户 09-14 报的「m66362111981 已经没了但还在命中里」也是它。

这里锁三件事：
  1. _call 的 soft 状态码：原样返回、不睡、不计失败、不推退避档
  2. detail() 认出这个形状 → None（＝删了，上层标 gone）；别的 403 照旧抛
  3. 这个形状必须【只认 code】，不认状态码 —— 真限流也可能是 403
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import base, mercari  # noqa: E402

DELETED = {"result": "error",
           "errors": [{"code": "InvisibleItemException", "message": "該当する商品は削除されています。"}],
           "meta": {"sub_error_code": "404"}}


class Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False) if body is not None else text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeClient:
    def __init__(self, resp):
        self.resp, self.calls = resp, 0

    def request(self, *a, **kw):
        self.calls += 1
        return self.resp


class Store:
    """记下 bump_daily 被怎么调的。"""
    def __init__(self):
        self.bumps = []

    def bump_daily(self, key, requests=0, errors=0):
        self.bumps.append((requests, errors))

    def get_settings(self, force=False):
        return {"req_delay_min": 0, "req_delay_max": 0, "daily_request_limit": 10 ** 9}

    def today_stat(self):
        return {"requests": 0}


def make_src(resp):
    src = mercari.Mercari.__new__(mercari.Mercari)
    src._client = FakeClient(resp)
    src._lock = base.threading.Lock()
    src._last_at = 0.0
    src._fails = 0
    src._headers = lambda method, url: {}
    return src


def run_call(resp, soft=()):
    src = make_src(resp)
    st = Store()
    saved_store, saved_sleep = base.store, base.time.sleep
    slept = []
    base.store = st
    base.time.sleep = lambda s: slept.append(s)
    try:
        out = src._call("GET", "https://x", soft=soft)
        return out, st, slept, src
    except Exception as e:
        return e, st, slept, src
    finally:
        base.store, base.time.sleep = saved_store, saved_sleep


# ---------------------------------------------------------------- 1. soft

def test_soft状态码原样返回_不睡不计失败不推档():
    out, st, slept, src = run_call(Resp(403, DELETED), soft=(403,))
    assert isinstance(out, Resp) and out.status_code == 403
    assert slept == [], "soft 的 403 不该退避睡觉"
    assert st.bumps == [(1, 0)], "要计一次请求，但【不】计失败"
    assert src._fails == 0, "不该推高退避档位"


def test_不在soft里的403照旧退避并抛():
    out, st, slept, src = run_call(Resp(403, DELETED), soft=())
    assert isinstance(out, base.RateLimited)
    assert slept == [60], "真限流要睡 60 秒"
    assert st.bumps == [(1, 1)]
    assert src._fails == 1


# ---------------------------------------------------------------- 2. detail

def run_detail(resp):
    src = make_src(resp)
    st = Store()
    saved_store, saved_sleep = base.store, base.time.sleep
    slept = []
    base.store = st
    base.time.sleep = lambda s: slept.append(s)
    try:
        return src.detail("m32209355337"), slept
    finally:
        base.store, base.time.sleep = saved_store, saved_sleep


def test_已删除的商品返回None_和404一样():
    """None 是"商品没了"的语义：追踪刷新、对账、拉描述三处都会把它标成 gone。"""
    d, slept = run_detail(Resp(403, DELETED))
    assert d is None
    assert slept == [], "删掉的商品不是限流，不该睡"


def test_真404也返回None():
    d, _ = run_detail(Resp(404, text="not found"))
    assert d is None


def test_没有InvisibleItemException的403才当限流():
    """【只认 code 不认状态码】真限流也可能回 403，那时候必须照旧抛出去让上层推迟。"""
    d_or_exc = None
    try:
        d_or_exc, _ = run_detail(Resp(403, {"result": "error", "errors": [{"code": "RateLimit"}]}))
    except base.RateLimited as e:
        d_or_exc = e
    assert isinstance(d_or_exc, base.RateLimited)


def test_403正文不是JSON也不当删除():
    try:
        d, _ = run_detail(Resp(403, text="<html>Forbidden</html>"))
    except base.RateLimited:
        return
    raise AssertionError(f"HTML 403 被当成了删除：{d!r}")


def test_交易中的商品照常读():
    """对照：trading 的商品接口回 200，别把它和删除混了。"""
    body = {"data": {"description": "x", "price": 460300, "name": "MSI 4090", "status": "trading",
                     "shipping_from_area": {"name": "東京都"}, "auction_info": {"total_bids": "3"}}}
    d, _ = run_detail(Resp(200, body))
    assert d["status"] == "trading" and d["price"] == 460300 and d["bid_count"] == 3


def test_deleted只认这个形状():
    assert mercari._deleted(Resp(403, DELETED)) is True
    assert mercari._deleted(Resp(403, {"errors": [{"code": "Other"}]})) is False
    assert mercari._deleted(Resp(403, {"errors": []})) is False
    assert mercari._deleted(Resp(403, {})) is False
    assert mercari._deleted(Resp(403, text="nope")) is False
