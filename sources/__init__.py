"""数据源注册表。

加新源＝在 sources/ 下写一个 Source 子类，然后在这里登记一行。
规则表的 sources 列填的就是这里的 key（留空＝全部源）。
"""
from sources.base import DailyLimitReached, RateLimited, Source  # noqa: F401
from sources.mercari import Mercari
from sources.yahoo_auction import YahooAuction
from sources.yahoo_flea import YahooFlea

_REGISTRY: dict[str, Source] = {}


def all_sources() -> dict[str, Source]:
    """进程内单例：每个源自带限速/退避状态，必须复用同一个实例。"""
    if not _REGISTRY:
        for cls in (Mercari, YahooFlea, YahooAuction):
            inst = cls()
            _REGISTRY[inst.key] = inst
    return _REGISTRY


def get(key: str) -> Source | None:
    return all_sources().get(key)


def for_rule(rule: dict) -> list[Source]:
    """这条规则要搜哪些源。sources 留空＝全部。"""
    want = [w.strip() for w in (rule.get("sources") or "").split(",") if w.strip()]
    srcs = all_sources()
    if not want:
        return list(srcs.values())
    return [srcs[w] for w in want if w in srcs]
