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
    # —— 面板显示 ——
    "fresh_hours": (
        int, 24,
        "【新上架】/【新发现】两个徽标的时间窗口（小时）。"
        "新上架＝商品本身挂出来不久（看平台给的上架时间）；"
        "新发现＝商品早就挂着，但我们刚把它抓进来（多半是它降价进了你的价格区间）。"
        "【ヤフオク 的搜索结果不给上架时间】所以它的商品只会有「新发现」，没有「新上架」。"
        "另外规则刚建起来的那一阵，库里所有东西都是刚抓到的，两个徽标会整个不显示 —— "
        "那时候全是新的，标了等于没标。",
    ),
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
        "每日请求上限保险丝：当天超过就停抓到次日 0 点（JST），"
        "而且是【所有规则所有源】一起停。\n"
        "估算公式：规则数 × Σ各源页数 × (1440 / quick_min) + 详情与成交轮。\n"
        "实测默认配置（2 条规则、3 个源、quick_min=7）：每轮 5 个搜索请求 × 约 197 轮/天"
        " × 2 条规则 ≈ 2000 次/天。\n"
        "【搜索只占一半，别照公式估】2026-09-13 实测 4 条规则（单卡×2 每7分钟 + "
        "整机×2 每30分钟）是 157 次/小时 ≈ 3770 次/天，而按页数算的搜索部分只有 106 次/小时。"
        "差出来的 50 次/小时是【售出对账】：每轮要对从搜索结果里消失的商品逐个拉详情核实，"
        "这是持续开销不是突发。所以估算时把公式算出来的数【乘以 1.5】才接近真实。\n"
        "【所以加第三条规则、或把 quick_min 从 7 调到 5，都会当天撞线】"
        "动这两个值之前先把上限一起调大。",
    ),
    # —— 追踪 ——
    "track_min": (
        int, 5,
        "追踪中的商品多久单独刷新一次（分钟）。\n"
        "【这是整个项目里唯一一处按件发请求的地方】平时商品的价格和状态只在整轮关键词"
        "扫描时更新（每条规则的 quick_min，默认 7 分钟，而且一轮要遍历所有源）；"
        "追踪的商品会直接拉它自己的详情页，所以快得多，拍卖的出价数也只有这条路能及时拿到。\n"
        "代价是每件每次刷新都是一个真实请求：追 10 件、间隔 5 分钟 ＝ 每天 2880 次，"
        "比现在四条规则的总量还大。【追之前先看看 daily_request_limit 还剩多少】",
    ),
    "track_budget": (
        int, 10,
        "每一轮最多刷新几件追踪商品。\n"
        "追的件数多过这个数时，按「最久没刷新的优先」轮着来 —— 不会漏掉谁，"
        "只是每件的实际间隔会被拉长。这道闸是防止你一口气追几十件就把配额烧穿。",
    ),

    # —— 推送 ——
    "notify_url": (
        str, "",
        "推送地址。【留空＝不推送，一个请求都不发】。\n"
        "填什么都行，常见的几家（都是实际接口形状）：\n"
        "  ntfy      https://ntfy.sh/你的主题          （请求体模板留空）\n"
        "  Bark      https://api.day.app/你的KEY       （请求体模板留空）\n"
        "  Discord   webhook 地址                      （模板见 notify_body）\n"
        "  企业微信  群机器人 webhook 地址             （模板见 notify_body）\n"
        "  Telegram  https://api.telegram.org/bot<TOKEN>/sendMessage\n"
        "【这个地址等于一把钥匙】谁拿到都能往你手机推东西，别写进截图或仓库。",
    ),
    "notify_body": (
        str, "",
        "请求体模板（JSON）。留空＝把提醒正文当纯文本直接发（ntfy / Bark 这么用）。\n"
        "要发 JSON 就在这里写，用 {text} 占位提醒正文，会自动转义：\n"
        '  Discord   {"content": "{text}"}\n'
        '  企业微信  {"msgtype":"text","text":{"content":"{text}"}}\n'
        '  Telegram  {"chat_id":"你的chat_id","text":"{text}"}',
    ),
    "notify_on": (
        str, "deal",
        "推什么：deal＝只推捡漏（低于市价中位数 × deal_ratio%）；matched＝所有命中都推。\n"
        "【默认只推捡漏是有意的】命中只是「符合你的条件」，捡漏才是「该立刻去看」。"
        "所有命中都推的话，一条规则几十件在售会让你很快关掉推送。\n"
        "还没攒够成交样本时算不出中位数，也就没有捡漏 —— 那段时间填 deal 会一条都不推。",
    ),
    "notify_max_per_round": (
        int, 5,
        "一轮最多推几条。【超过就一条都不推，只把它们标成已推】——\n"
        "挡的是三种一次性井喷：刚填上 notify_url（库里几十件老命中一起轰出来）、"
        "停机几天后重启（攒下的过期货）、放宽规则或改价格区间（几百件同时变命中）。\n"
        "代价是真有一波好货时会被整批跳过，但那种情况面板上看得到；"
        "被几十条通知淹掉的人只会直接把推送关了。",
    ),
}

# 面板上怎么显示这些设置项：分组 → [(键, 中文名), ...]
#
# 【为什么不塞进 SETTINGS_SPEC 的元组里】那个元组被 store.py 在四处解包成
# (typ, default, note)，加一项就得同步改四处，而这里加的纯粹是界面文案，
# 跟取值、类型、落库都没关系。分开放，改 UI 文案不碰数据层。
#
# 【为什么中文名旁边还要留英文键】日志、报错、规则页的备注里引用的都是英文键
# （"先把 daily_request_limit 调大"），只显示中文名的话对不上号。
#
# 【顺序就是页面顺序】从"最常改的"到"基本不动的"排。
# 漏写一项会被 tests/test_settings_ui.py 拦下来 —— 漏了的表现是它在面板上
# 整个消失（不在任何分组里就不会被渲染），而库里的值照常生效，很难发现。
SETTING_GROUPS: dict[str, list[tuple[str, str]]] = {
    "抓取节奏（直接决定会不会被风控）": [
        ("req_delay_min", "请求间隔下限（秒）"),
        ("req_delay_max", "请求间隔上限（秒）"),
        ("daily_request_limit", "每日请求上限（次）"),
        ("max_pages", "每轮最多翻几页"),
        ("detail_budget", "详情请求预算（次）"),
        ("missing_grace_min", "失踪多久才去核实（分钟）"),
        ("sold_scan_hours", "成交轮间隔（小时）"),
    ],
    "市价基准": [
        ("median_window_days", "中位数统计窗口（天）"),
        ("median_min_samples", "出中位数最少要几件成交"),
    ],
    "追踪": [
        ("track_min", "追踪刷新间隔（分钟）"),
        ("track_budget", "每轮最多刷新几件"),
    ],
    "推送": [
        ("notify_url", "推送地址"),
        ("notify_body", "请求体模板（JSON）"),
        ("notify_on", "推什么"),
        ("notify_max_per_round", "一轮最多推几条"),
    ],
    "面板显示": [
        ("fresh_hours", "「新上架/新发现」时间窗（小时）"),
    ],
}

LOG_DIR = BASE_DIR / "logs"
