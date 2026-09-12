"""数据源抽象。

每个源自己负责「怎么搜、怎么解析」，这一层统一管住所有源都要守的事：
全局串行、请求间随机间隔、429/403 指数退避、连续失败熔断、每日请求总量保险丝。

【每个源的限速状态是独立的】退避档位、上次请求时间、锁都是实例级，Mercari 的
连续失败不会让 Yahoo 进入退避。但要清楚：轮询只有【一条线程】，串行遍历
规则×源，而退避是在这条线程里真的 sleep 的 —— 所以 Mercari 退避 300 秒期间，
Yahoo 那一轮确实会被顺延。这是"全局串行、绝不并发"这个前提的必然代价，
换来的是任何时刻对外只有一个请求在飞。别把"状态独立"读成"时间上互不影响"。

每日总量上限是【跨源合计】的一个数 —— 那管的是「今天一共对外发了多少请求」。
"""
import logging
import random
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime

import httpx

import config
from db import store

log = logging.getLogger("source")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")


class RateLimited(Exception):
    """被限流/拒绝。调用方应当结束本轮，不要重试。"""


class DailyLimitReached(Exception):
    """当天请求总量到顶。"""


class Source(ABC):
    key: str = ""          # 落库用的短名，如 mercari / yahoo_flea
    name: str = ""         # 面板上显示的名字

    def __init__(self) -> None:
        self._client = httpx.Client(http2=True, timeout=20.0, follow_redirects=True)
        # 每个源一把自己的锁：保证本源串行（面板手动试跑不会和轮询线程撞在一起），
        # 同时不牵连别的源。
        self._lock = threading.Lock()
        self._last_at = 0.0
        self._fails = 0

    # ------------------------------------------------------------ 子类实现

    @abstractmethod
    def search(self, keyword: str, *, sold: bool = False, page_token: str = "") -> dict:
        """返回 {'items': [snap…], 'next': str, 'total': int}。

        snap 必须包含：source/item_id/name/price/status/condition_id/item_type/
        category_id/brand_name/seller_id/thumb_url/listed_at/updated_at_src
        """

    @abstractmethod
    def detail(self, item_id: str) -> dict | None:
        """返回 {'description', 'price', 'name', 'status', 'ship_from'}；商品已删除时返回 None。

        ship_from 是发货地都道府县（如「東京都」），取不到就给空串 ——
        三个源都【只在详情里】给这个字段，搜索结果里一律没有。
        """

    @abstractmethod
    def item_url(self, item_id: str) -> str:
        """商品页地址，面板上点标题跳过去。"""

    def _headers(self, method: str, url: str) -> dict:
        """默认头。需要签名的源（如 Mercari 的 DPoP）覆盖这个方法。"""
        return {"user-agent": UA, "accept": "*/*", "accept-language": "ja,en;q=0.9"}

    # ------------------------------------------------------------ 公共的请求纪律

    def _pace(self) -> None:
        """本源两次请求之间的随机间隔。以「上次请求结束」为基准，不是固定睡 N 秒。"""
        st = store.get_settings()
        lo, hi = st["req_delay_min"], max(st["req_delay_max"], st["req_delay_min"])
        wait = random.uniform(lo, hi) - (time.time() - self._last_at)
        if wait > 0:
            time.sleep(wait)

    def _check_quota(self) -> None:
        # 上限管的是【全源合计】：三个源各发各的，只看单源的话总量会悄悄翻三倍。
        limit = store.get_settings()["daily_request_limit"]
        total = store.today_stat()["requests"]
        if total >= limit:
            raise DailyLimitReached(f"当天全源已发 {total} 次请求，到上限 {limit}")

    def _call(self, method: str, url: str, *, params=None, json_body=None) -> httpx.Response:
        with self._lock:
            self._check_quota()
            self._pace()
            try:
                resp = self._client.request(method, url, params=params, json=json_body,
                                            headers=self._headers(method, url))
            except httpx.HTTPError as e:
                # 【不累加 _fails】_fails 是"被限流了几次"的计数，只用来算退避档位。
                # 网络抖动混进来的话，几次超时就能把下一次真限流的退避直接顶到 300 秒上限。
                store.bump_daily(self.key, requests=1, errors=1)
                raise RateLimited(f"{self.name} 网络错误：{e}") from e
            finally:
                self._last_at = time.time()

            if resp.status_code == 200:
                self._fails = 0
                store.bump_daily(self.key, requests=1)
                return resp

            if resp.status_code == 404:
                # 商品被删了是【正常事件】，不是错误：既不该计进面板的"失败 N"
                # （那会让这个数字失去诊断价值），也不该推高退避档位。
                store.bump_daily(self.key, requests=1)
                return resp
            store.bump_daily(self.key, requests=1, errors=1)
            self._fails += 1
            if resp.status_code in (429, 403, 503):
                # 退避到 5 分钟封顶。这里是真的睡着不动 —— 被限流时继续发请求只会更糟。
                back = min(60 * 2 ** (self._fails - 1), 300)
                log.warning("%s 被限流 HTTP %s，退避 %d 秒（连续失败 %d 次）",
                            self.name, resp.status_code, back, self._fails)
                time.sleep(back)
                self._last_at = time.time()
                raise RateLimited(f"{self.name} HTTP {resp.status_code}")
            raise RateLimited(f"{self.name} HTTP {resp.status_code}: {resp.text[:200]}")

    # ------------------------------------------------------------ 工具

    @staticmethod
    def ts(v) -> datetime | None:
        """unix 秒 → naive JST，和库里所有时间列的口径一致。"""
        if not v:
            return None
        try:
            return datetime.fromtimestamp(int(v), config.JST).replace(tzinfo=None)
        except (ValueError, OSError, OverflowError):
            return None

    @staticmethod
    def iso(v) -> datetime | None:
        """ISO8601（带时区）→ naive JST。Yahoo 给的是 2026-09-08T17:53:46+09:00 这种。"""
        if not v:
            return None
        try:
            return datetime.fromisoformat(v).astimezone(config.JST).replace(tzinfo=None)
        except (ValueError, TypeError):
            return None
