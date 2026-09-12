"""配置。结构项（连库、端口、节流）走 .env，改了要重启；
业务规则（关键词、价格区间、排除词）在数据库 watch_rule 表里，面板上改，下一轮即时生效。
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# 各平台的时间全是 JST，所有落库时间统一按 JST 写。
# 不用 MySQL 的 NOW()：数据库、开发机、部署机三边时区不保证一致。
JST = timezone(timedelta(hours=9))


def now() -> datetime:
    """当前 JST 时间，去掉 tzinfo —— MySQL 的 DATETIME 不存时区，带着反而会被驱动警告。"""
    return datetime.now(JST).replace(tzinfo=None)


def _int(key: str, default: int) -> int:
    try:
        v = int(os.getenv(key, "") or default)
    except ValueError:
        return default
    return v if v > 0 else default


# 默认值一律是无害占位：真实连接信息只存在于 .env（已在 .gitignore 里）。
# 这样万一 .env 丢了，进程会连本地失败并报错，而不是悄悄连上别的机器。
DB = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": _int("DB_PORT", 3306),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "resale_watcher"),
}

WEB_PORT = _int("WEB_PORT", 2334)
WEB_HOST = os.getenv("WEB_HOST") or "127.0.0.1"

# ---- 可热改设置：{键: (类型, 默认值, 说明)} ----
# 这些【不进 .env】。建库时写进 app_setting 表，之后以数据库为唯一来源，
# 面板「设置」页改完 10 秒内生效。说明文字会一并写进表的 note 列，
# 所以用 Navicat / DBeaver 直接开表也看得懂每一项是干什么的。
SETTINGS_SPEC: dict[str, tuple[type, object, str]] = {
    # —— 市价基准 ——
    "median_window_days": (
        int, 30,
        "市价中位数只统计最近这么多天的成交。窗口外的成交样本会被自动删掉。"
        "实测 5090 全时段中位数 ¥590,100、近30天 ¥720,000——显卡在涨价，窗口开太大会失真。",
    ),
    "median_min_samples": (
        int, 5,
        "成交样本少于这么多件就不出中位数（面板显示「样本不足」，也不做捡漏判定）。"
        "实测搜 RTX 5090 的成交价从 ¥2,000 到 ¥998,000 都有，样本少时中位数会被一两件废品带偏。",
    ),
    # —— 抓取节奏（直接决定会不会被风控）——
    "req_delay_min": (float, 3.0, "两个 API 请求之间的最小随机等待（秒）。"),
    "req_delay_max": (float, 8.0, "两个 API 请求之间的最大随机等待（秒）。"),
    "max_pages": (
        int, 5,
        "单条规则每轮最多翻几页（每页约 110 件）。实测「RTX 5090」全网在售仅 188 件、2 页扫完。"
        "只有当关键词宽到商品数超过 页数×110 时才会被截断，日志和面板都会告警——"
        "被截断时售出对账会自动跳过（没扫全的话「没出现」不等于「卖掉了」）。",
    ),
    "sold_scan_hours": (int, 24, "成交轮间隔（小时）：多久抓一次成交价重算市价中位数。"),
    "detail_budget": (
        int, 10,
        "详情请求预算。【实际是「每个源、每个阶段」各一份】：售出对账花一份、"
        "补描述花一份，再乘以启用的源数 —— 三个源就是最多 6 倍这个数。"
        "稳态下每轮只有个位数新商品，远用不满；这个数是给「规则刚建立、一次涌进几十件」兜底的。",
    ),
    "missing_grace_min": (
        int, 20,
        "商品从搜索结果里消失多久之后（分钟）才去核实它是不是卖掉了。"
        "不设宽限期的话，分页抖动造成的单轮缺席会让每轮都多打十几个详情请求。",
    ),
    "daily_request_limit": (
        int, 3000,
        "每日请求上限保险丝：当天超过就停抓到次日 0 点（JST）。"
        "按默认节奏两条规则一天约 400〜500 次。",
    ),
}

LOG_DIR = BASE_DIR / "logs"
