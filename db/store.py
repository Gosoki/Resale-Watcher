"""数据库访问。全部手写 SQL，每次操作开一条短连接。

【为什么不做连接池/长连接】轮询线程和 NiceGUI 的事件循环会同时读写，
PyMySQL 的连接不是线程安全的，共享一条必然在某天撞出 "Packet sequence number wrong"。
局域网内建连接约几毫秒，而我们每轮只有几十次查询，省这点开销换来的并发 bug 不划算。
"""
import contextlib
import re
import statistics
import time
from datetime import timedelta

import logging

import pymysql
from pymysql.cursors import DictCursor

import config

log = logging.getLogger("store")

SCHEMA_PATH = config.BASE_DIR / "db" / "schema.sql"


@contextlib.contextmanager
def conn(db: bool = True):
    """db=False 时不指定库名，用于建库本身。"""
    kw = dict(config.DB, charset="utf8mb4", cursorclass=DictCursor, autocommit=True)
    if not db:
        kw.pop("database")
    c = pymysql.connect(**kw)
    try:
        yield c
    finally:
        c.close()


# 【args 为空时必须不传】PyMySQL 只要 args 不是 None 就会拿 SQL 去做 % 格式化，
# 而我们的 DDL 注释里有「±20%」「百分之多少」这类字符 —— 一执行就报
# "not enough arguments for format string"。这个坑只在无参数 SQL 上出现，
# 平时带参数的查询碰不到，所以藏得很深。
def query(sql: str, args=()) -> list[dict]:
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, args) if args else cur.execute(sql)
        return cur.fetchall()


def one(sql: str, args=()) -> dict | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args=()) -> int:
    """返回受影响行数。args 为空时不传给驱动，理由见上面 query 的注释。"""
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, args) if args else cur.execute(sql)
        return cur.rowcount


# ---------------------------------------------------------------- 建库

def init_schema() -> None:
    """执行 schema.sql。全是 IF NOT EXISTS，可反复跑。"""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    # 【库名以 .env 为准】schema.sql 里写死了 CREATE DATABASE / USE 的库名，
    # 不替换的话 .env 的 DB_NAME 改了等于没改：建表建到 schema.sql 写的那个库里，
    # 而连接用的是 .env 的库名，启动时报 Unknown database —— 而 deploy.sh
    # 的错误提示还把原因指向密码和网络，排查方向全歪。
    sql = sql.replace("resale_watcher", config.DB["database"])
    # 先按行剥掉整行注释，再按分号切分。
    # 【不能反过来】先切分再判断"整段是否以 -- 开头"会把 CREATE DATABASE 一起丢掉——
    # 它前面正好压着一大段注释，strip 后整段就是以 -- 开头的，于是建库语句被静默跳过，
    # 接着 USE 报 Unknown database。schema.sql 里没有行尾注释，也没有存储过程，
    # 所以按行剥注释 + 朴素切分是安全的。
    body = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
    stmts = [s.strip() for s in body.split(";") if s.strip()]
    with conn(db=False) as c, c.cursor() as cur:
        for s in stmts:
            cur.execute(s)
    sync_schema()
    seed_settings()


_COL_LINE = re.compile(r"^\s{2}(\w+)\s+.*COMMENT\s+'", re.I)


def plan_schema_changes(want: dict, have: dict) -> tuple[list, list]:
    """对着 schema.sql（want）和库里的现状（have）算出要执行哪些 ALTER。

    拆成纯函数是为了能离线测 —— 尤其是「只增不删」这条：库里有、schema.sql
    里没有的列必须原样留着。自动 DROP 一列就是不可逆地删数据，这个函数
    永远不该产出 DROP。

    want: {(表, 列): schema.sql 里那一行完整定义}
    have: {(表, 列): 该列在库里的 COMMENT}
    返回 (要新增的, 要改注释的)，元素都是 (表, 列, 定义)。
    """
    tables = {t for t, _ in have}          # 库里已经存在的表
    adds, mods = [], []
    for (table, col), definition in want.items():
        if table not in tables:
            continue                        # 整张表还没建：CREATE TABLE 会连列带注释一起建出来
        if (table, col) not in have:
            adds.append((table, col, definition))
            continue
        m = re.search(r"COMMENT\s+'((?:[^']|'')*)'", definition)
        if m and m.group(1).replace("''", "'") != have[(table, col)]:
            mods.append((table, col, definition))
    return adds, mods


def sync_schema() -> None:
    """把 schema.sql 的列定义同步到【已经存在】的表：补缺的列、改变了的注释。

    【为什么需要这一步】CREATE TABLE IF NOT EXISTS 只在建表那一刻生效；
    之后你往 schema.sql 里加一列、或改一句 COMMENT，对已有的库毫无作用。
    注释分叉的后果是仓库里的文档和你在 Navicat 里看到的说明对不上；
    少一列的后果更直接 —— 代码 SELECT 它的时候直接报 Unknown column，
    而唯一的出路要么手写 ALTER，要么删库重建（那会丢掉攒了几天的成交样本，
    也就是市价中位数的全部依据）。

    做法是直接拿 schema.sql 里那一行原文去 ADD / MODIFY COLUMN，
    所以 schema.sql 始终是唯一真相。

    【只增不删】库里有、schema.sql 里没有的列一律不动。自动 DROP 一列
    等于不可逆地删数据，宁可留着一列没人用的垃圾。
    【只认带 COMMENT 的列】_COL_LINE 要求行里有 COMMENT，这也是本项目的约定：
    每个有意义的列都写注释。不带注释的列（自增主键之类）不参与同步。
    """
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    # 【库名以 .env 为准】schema.sql 里写死了 CREATE DATABASE / USE 的库名，
    # 不替换的话 .env 的 DB_NAME 改了等于没改：建表建到 schema.sql 写的那个库里，
    # 而连接用的是 .env 的库名，启动时报 Unknown database —— 而 deploy.sh
    # 的错误提示还把原因指向密码和网络，排查方向全歪。
    sql = sql.replace("resale_watcher", config.DB["database"])
    want: dict[tuple[str, str], str] = {}     # (表, 列) -> 该列在 schema.sql 里的完整定义
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\) ENGINE", sql, re.S):
        table, body = m.group(1), m.group(2)
        for line in body.splitlines():
            hit = _COL_LINE.match(line)
            if hit:
                want[(table, hit.group(1))] = line.strip().rstrip(",")

    have = {(r["TABLE_NAME"], r["COLUMN_NAME"]): r["COLUMN_COMMENT"]
            for r in query("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT "
                           "FROM information_schema.columns WHERE table_schema = %s",
                           (config.DB["database"],))}
    adds, mods = plan_schema_changes(want, have)
    for table, col, definition in adds:
        # 【补列会走一遍全表】几百行的表眨眼就完，但表大了之后这一句会卡住启动，
        # 所以日志里把每一列都点名，出问题时一眼看得出是谁在动表。
        log.warning("表 %s 缺列 %s，按 schema.sql 补上", table, col)
        execute(f"ALTER TABLE `{table}` ADD COLUMN {definition}")
    for table, col, definition in mods:
        execute(f"ALTER TABLE `{table}` MODIFY COLUMN {definition}")
    if adds:
        log.info("已补 %d 列", len(adds))
    if mods:
        log.info("已把 %d 列的注释同步成 schema.sql 里的版本", len(mods))


# ---------------------------------------------------------------- 全局设置

_SETTINGS_TTL = 10.0                      # 秒。改完设置最多 10 秒生效，不用重启
_settings_cache: dict = {"at": 0.0, "data": {}}


def seed_settings() -> None:
    """把 config.SETTINGS_SPEC 里的项补进 app_setting 表。

    note 每次都按代码里的说明覆盖（说明文字跟着代码走），但 v 只在首次插入时写 ——
    否则每次重启都会把你在面板上改过的值冲回默认值。
    """
    now = config.now()
    for k, (_typ, default, note) in config.SETTINGS_SPEC.items():
        execute("INSERT INTO app_setting (k, v, note, updated_at) VALUES (%s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE note = VALUES(note)",
                (k, str(default), note, now))
    # 代码里删掉某个设置项后，表里会留下一行没人读的孤儿 —— 它还会显示在面板
    # 「设置」页上让你以为改了有用。跟着 SPEC 一起清掉。
    keys = list(config.SETTINGS_SPEC)
    execute(f"DELETE FROM app_setting WHERE k NOT IN ({','.join(['%s'] * len(keys))})", keys)


def get_settings(force: bool = False) -> dict:
    """全局设置，按 SETTINGS_SPEC 声明的类型转好。带 10 秒缓存 ——
    每轮判定都要读它，不缓存的话一轮几百次查询全打在数据库上。"""
    now = time.monotonic()
    if not force and _settings_cache["data"] and now - _settings_cache["at"] < _SETTINGS_TTL:
        return _settings_cache["data"]
    try:
        raw = {r["k"]: r["v"] for r in query("SELECT k, v FROM app_setting")}
    except Exception:                      # noqa: BLE001 - 库抽风时用默认值顶着，别让判定逻辑崩掉
        raw = {}
    data = {}
    for k, (typ, default, _note) in config.SETTINGS_SPEC.items():
        v = raw.get(k)
        if v is None:
            data[k] = default
            continue
        try:
            # int("20.0") 会抛；先过 float 再收成 int，兼容历史上已经存成 "20.0" 的行
            data[k] = int(float(v)) if typ is int else typ(v)
        except (TypeError, ValueError):
            # 【必须出声】以前这里是静默回落，于是"改了设置没生效"完全没有线索可查
            log.warning("设置项 %s 的值 %r 不是合法的 %s，本次回落到默认值 %r",
                        k, v, typ.__name__, default)
            data[k] = default
    _settings_cache.update(at=now, data=data)
    return data


def all_settings() -> list[dict]:
    """给面板用：带 note 和当前值的完整列表，顺序按 SETTINGS_SPEC。"""
    raw = {r["k"]: r for r in query("SELECT * FROM app_setting")}
    cur = get_settings(force=True)
    return [{"k": k, "v": cur[k], "note": (raw.get(k) or {}).get("note", note),
             "type": typ.__name__, "default": default}
            for k, (typ, default, note) in config.SETTINGS_SPEC.items()]


def save_setting(k: str, v) -> None:
    if k not in config.SETTINGS_SPEC:
        raise KeyError(f"未知设置项：{k}")
    # 【面板传来的数字是 float】NiceGUI 的 ui.number 永远给 float，直接 str() 存进去
    # 就是 "20.0"，读回来 int("20.0") 抛 ValueError → 被 get_settings 吞掉 → 静默回落默认值。
    # 后果是面板上所有 int 型设置（max_pages / detail_budget / daily_request_limit …）
    # 改了等于没改，而且没有任何提示。存之前先按声明的类型规范化。
    typ = config.SETTINGS_SPEC[k][0]
    try:
        v = int(float(v)) if typ is int else (float(v) if typ is float else v)
    except (TypeError, ValueError):
        raise ValueError(f"{k} 需要 {typ.__name__}，收到 {v!r}") from None
    execute("INSERT INTO app_setting (k, v, note, updated_at) VALUES (%s, %s, '', %s) "
            "ON DUPLICATE KEY UPDATE v = VALUES(v), updated_at = VALUES(updated_at)",
            (k, str(v), config.now()))
    _settings_cache["at"] = 0.0            # 立即失效，下次读就是新值


# ---------------------------------------------------------------- 规则

RULE_COLUMNS = (
    "name, enabled, keyword, sources, include_all, include_any, exclude_any, warn_desc, "
    "price_min, price_max, condition_ids, allow_shops, check_desc, "
    "deal_ratio, quick_min, note"
)
# 精确的列名元组。别拿上面那个字符串做成员判断 ——「id」是「condition_ids」的子串，
# `"id" in RULE_COLUMNS` 会返回 True。
RULE_FIELDS = tuple(c.strip() for c in RULE_COLUMNS.split(","))


def _with_settings(rule: dict) -> dict:
    """把全局设置并进规则字典。

    这样 core/matcher.py 不用 import store 就能拿到 max_pages 之类的全局项，
    保持纯函数、好测试；调用方也不必到处传 settings 参数。
    规则自身的字段优先（虽然目前两边没有重名）。写回时 update_rule 只认
    RULE_FIELDS，多出来的键不会被误写进 watch_rule 表。
    """
    return {**get_settings(), **rule}


def get_rules(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM watch_rule"
    if enabled_only:
        sql += " WHERE enabled = 1"
    return [_with_settings(r) for r in query(sql + " ORDER BY id")]


def get_rule(rule_id: int) -> dict | None:
    r = one("SELECT * FROM watch_rule WHERE id = %s", (rule_id,))
    return _with_settings(r) if r else None


def insert_rule(data: dict) -> int:
    cols = [c.strip() for c in RULE_COLUMNS.split(",")]
    ph = ", ".join(["%s"] * len(cols))
    sql = (f"INSERT INTO watch_rule ({RULE_COLUMNS}, created_at, updated_at) "
           f"VALUES ({ph}, %s, %s)")
    args = [data.get(c) for c in cols] + [config.now(), config.now()]
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, args)
        return cur.lastrowid


def update_rule(rule_id: int, data: dict) -> None:
    cols = [c.strip() for c in RULE_COLUMNS.split(",") if c.strip() in data]
    if not cols:
        return
    sets = ", ".join(f"{c} = %s" for c in cols) + ", updated_at = %s"
    execute(f"UPDATE watch_rule SET {sets} WHERE id = %s",
            [data[c] for c in cols] + [config.now(), rule_id])


def delete_rule(rule_id: int) -> None:
    # item / sold_sample 不挂外键（见 schema.sql），删规则时手动清干净，
    # 免得留下一堆 rule_id 指向不存在规则的孤儿行，把面板的统计数字算错。
    execute("DELETE FROM item WHERE rule_id = %s", (rule_id,))
    execute("DELETE FROM sold_sample WHERE rule_id = %s", (rule_id,))
    execute("DELETE FROM watch_rule WHERE id = %s", (rule_id,))
    # price_log 不带 rule_id，没法按规则删；改成清掉「已经没有任何 item 行引用」的记录。
    # 不清的话这张表只涨不减，删一条抓过几百件商品的规则会留下等量的孤儿。
    execute("DELETE pl FROM price_log pl "
            "LEFT JOIN item i ON i.source = pl.source AND i.item_id = pl.item_id "
            "WHERE i.item_id IS NULL")
    # 兜底：轮询线程可能正好在删除的同时往这两张表写（没有事务，各自自动提交），
    # 那一瞬间写进去的行会指向一条已经不存在的规则。再扫一遍清掉。
    execute("DELETE FROM item WHERE rule_id NOT IN (SELECT id FROM watch_rule)")
    execute("DELETE FROM sold_sample WHERE rule_id NOT IN (SELECT id FROM watch_rule)")


# ---------------------------------------------------------------- 规则状态

def get_state(rule_id: int) -> dict:
    """规则级状态：只有跨源合并算出来的市价中位数。"""
    st = one("SELECT * FROM rule_state WHERE rule_id = %s", (rule_id,))
    if st is None:
        execute("INSERT IGNORE INTO rule_state (rule_id, updated_at) VALUES (%s, %s)",
                (rule_id, config.now()))
        st = one("SELECT * FROM rule_state WHERE rule_id = %s", (rule_id,))
    return st


def update_state(rule_id: int, **fields) -> None:
    get_state(rule_id)
    sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = %s"
    execute(f"UPDATE rule_state SET {sets} WHERE rule_id = %s",
            list(fields.values()) + [config.now(), rule_id])


def get_source_state(rule_id: int, source: str) -> dict:
    """规则 × 数据源的轮询进度。Mercari 刚扫完不代表 Yahoo 也扫完了，所以按源分开记。"""
    st = one("SELECT * FROM rule_source_state WHERE rule_id = %s AND source = %s",
             (rule_id, source))
    if st is None:
        execute("INSERT IGNORE INTO rule_source_state (rule_id, source, updated_at) "
                "VALUES (%s, %s, %s)", (rule_id, source, config.now()))
        st = one("SELECT * FROM rule_source_state WHERE rule_id = %s AND source = %s",
                 (rule_id, source))
    return st


def update_source_state(rule_id: int, source: str, **fields) -> None:
    get_source_state(rule_id, source)
    sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = %s"
    execute(f"UPDATE rule_source_state SET {sets} WHERE rule_id = %s AND source = %s",
            list(fields.values()) + [config.now(), rule_id, source])


# ---------------------------------------------------------------- 商品

ITEM_SNAP_COLS = ("name", "price", "status", "condition_id", "item_type",
                  "category_id", "brand_name", "seller_id", "thumb_url", "listed_at",
                  "end_time", "bid_count", "buy_now_price")


def get_item(source: str, item_id: str, rule_id: int) -> dict | None:
    return one("SELECT * FROM item WHERE source = %s AND item_id = %s AND rule_id = %s",
               (source, item_id, rule_id))


def upsert_item(rule_id: int, snap: dict, verdict: dict) -> dict:
    """写入/更新一件商品，返回 {'new': bool, 'price_changed': bool, 'old_price': int|None}。

    snap 来自搜索结果，verdict 是匹配判定（matched/reject_reason）。
    description 不在这里动 —— 那是详情阶段的事，每轮用 NULL 覆盖会把已拉到的描述抹掉。
    """
    now = config.now()
    src = snap["source"]
    old = get_item(src, snap["item_id"], rule_id)

    # 【这里曾经有一段"描述否决优先于标题复判"的保护】描述改成只打标签、不再否决之后
    # 就不需要了：desc_warn 是独立的一列，本函数根本不碰它，标题复判覆盖不掉。

    if old is None:
        cols = ", ".join(ITEM_SNAP_COLS)
        ph = ", ".join(["%s"] * len(ITEM_SNAP_COLS))
        execute(
            f"INSERT INTO item (source, item_id, rule_id, {cols}, first_price, min_price, "
            f"first_seen_at, last_seen_at, matched, reject_reason) "
            f"VALUES (%s, %s, %s, {ph}, %s, %s, %s, %s, %s, %s)",
            [src, snap["item_id"], rule_id] + [snap.get(c) for c in ITEM_SNAP_COLS]
            + [snap["price"], snap["price"], now, now,
               verdict["matched"], verdict["reject_reason"]],
        )
        add_price_log(src, snap["item_id"], snap["price"], now)
        return {"new": True, "price_changed": False, "old_price": None}

    changed = old["price"] != snap["price"]
    sets = ", ".join(f"{c} = %s" for c in ITEM_SNAP_COLS)
    execute(
        f"UPDATE item SET {sets}, min_price = LEAST(min_price, %s), last_seen_at = %s, "
        f"matched = %s, reject_reason = %s "
        f"WHERE source = %s AND item_id = %s AND rule_id = %s",
        [snap.get(c) for c in ITEM_SNAP_COLS]
        + [snap["price"], now, verdict["matched"], verdict["reject_reason"],
           src, snap["item_id"], rule_id],
    )
    if changed:
        add_price_log(src, snap["item_id"], snap["price"], now)
    return {"new": False, "price_changed": changed, "old_price": old["price"]}


def touch_seen(source: str, item_id: str, rule_id: int) -> None:
    """把 last_seen_at 推到现在。

    【这是售出对账能收敛的关键】last_seen_at 原本只在 upsert_item 里更新，
    也就是只有"出现在搜索结果里"才算见过。于是「搜不到、但详情说它还在售」的商品
    会永远满足 last_seen_at < cutoff，每一轮都被重新核实一次，永不收敛 ——
    攒够 detail_budget 个这种僵尸，真正卖掉的商品就再也轮不到核实，
    最后把每日请求配额烧穿、全源停抓。
    刚调过详情＝刚确认过它的状态，这时候更新 last_seen_at 是准确的语义。
    """
    execute("UPDATE item SET last_seen_at = %s "
            "WHERE source = %s AND item_id = %s AND rule_id = %s",
            (config.now(), source, item_id, rule_id))


def save_detail(source: str, item_id: str, rule_id: int, description: str, desc_warn: str) -> None:
    """存描述 + 警示标签。【不动 matched】描述只提示，不参与合不合适的判定。"""
    execute("UPDATE item SET description = %s, desc_checked = 1, desc_warn = %s "
            "WHERE source = %s AND item_id = %s AND rule_id = %s",
            (description, desc_warn, source, item_id, rule_id))


def set_deal(source: str, item_id: str, rule_id: int, is_deal: int, deal_pct: int | None) -> None:
    execute("UPDATE item SET is_deal = %s, deal_pct = %s "
            "WHERE source = %s AND item_id = %s AND rule_id = %s",
            (is_deal, deal_pct, source, item_id, rule_id))


def set_status(source: str, item_id: str, rule_id: int, status: str, sold_at=None) -> None:
    if sold_at is None:
        execute("UPDATE item SET status = %s "
                "WHERE source = %s AND item_id = %s AND rule_id = %s",
                (status, source, item_id, rule_id))
    else:
        execute("UPDATE item SET status = %s, sold_at = %s "
                "WHERE source = %s AND item_id = %s AND rule_id = %s",
                (status, sold_at, source, item_id, rule_id))


def on_sale_items(rule_id: int, source: str) -> list[dict]:
    """这条规则在这个源下仍标为在售的商品 —— 扫完要拿它和搜索结果对账，找出卖掉/下架的。

    【必须按 last_seen_at 升序】对账预算有限（detail_budget），没有排序时数据库返回顺序
    是稳定的，于是同一批"核实不出结果"的商品会每轮都排在前面、把预算吃光，
    真正卖掉的商品排在后面永远轮不到核实。升序＝最久没见到的优先，保证轮转。
    """
    # 【trading 也要捞】「交易中」不是终态：Mercari 上买家付款后商品会先变 trading，
    # 成交后才变 sold_out。只捞 on_sale 的话，一件商品被标成 trading 之后就
    # 再也不会被对账碰到，永远推进不到 sold_out —— tracked 成交样本（最准的那种）
    # 就此断流，而 trading 恰恰是最接近成交的状态。
    return query("SELECT item_id, price, matched, last_seen_at, status FROM item "
                 "WHERE rule_id = %s AND source = %s AND status IN ('on_sale', 'trading') "
                 "ORDER BY last_seen_at ASC", (rule_id, source))


def pending_detail(rule_id: int, source: str, limit: int) -> list[dict]:
    """初筛通过但还没拉过详情的，按新到旧。limit 防止规则刚建时一口气拉几百个详情。"""
    return query("SELECT item_id, name FROM item WHERE rule_id = %s AND source = %s "
                 "AND matched = 1 AND desc_checked = 0 AND status = 'on_sale' "
                 "ORDER BY first_seen_at DESC LIMIT %s", (rule_id, source, limit))


def add_price_log(source: str, item_id: str, price: int, at) -> None:
    """记一条价格变动。

    【去重】price_log 不带 rule_id（价格是商品自身的属性），而 upsert_item 是
    按规则调的 —— 同一件商品被两条规则同时命中时会各调一次，写出两行一模一样的
    记录。这里比一下最新的那条，价格没变就不写。
    """
    last = one("SELECT price FROM price_log WHERE source = %s AND item_id = %s "
               "ORDER BY noted_at DESC, id DESC LIMIT 1", (source, item_id))
    if last and last["price"] == price:
        return
    execute("INSERT INTO price_log (source, item_id, price, noted_at) VALUES (%s, %s, %s, %s)",
            (source, item_id, price, at))


# ---------------------------------------------------------------- 成交样本 / 市价

def add_sold_sample(rule_id: int, source: str, item_id: str, price: int,
                    sold_at, sample_kind: str) -> None:
    # tracked 比 scan 准（拉过详情、过了完整规则），所以 tracked 可以覆盖同一件的 scan 记录，反之不行。
    execute(
        "INSERT INTO sold_sample (rule_id, source, item_id, price, sold_at, sample_kind) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE "
        "  price       = IF(VALUES(sample_kind) = 'tracked', VALUES(price),   price), "
        "  sold_at     = IF(VALUES(sample_kind) = 'tracked', VALUES(sold_at), sold_at), "
        "  sample_kind = IF(VALUES(sample_kind) = 'tracked', 'tracked',       sample_kind)",
        (rule_id, source, item_id, price, sold_at, sample_kind))


def refresh_median(rule_id: int) -> tuple[int | None, int]:
    """按窗口内的成交样本重算中位数，写回 rule_state，返回 (中位数, 样本数)。"""
    st = get_settings()
    since = config.now() - timedelta(days=st["median_window_days"])
    rows = query("SELECT price FROM sold_sample WHERE rule_id = %s AND sold_at >= %s",
                 (rule_id, since))
    prices = [r["price"] for r in rows]
    # 【必须先判非空】median_min_samples 设成 0 时 0 >= 0 成立，
    # statistics.median([]) 抛 StatisticsError，成交轮每次都会在最后一步炸掉。
    median = (int(statistics.median(prices))
              if prices and len(prices) >= st["median_min_samples"] else None)
    update_state(rule_id, median_price=median, sample_count=len(prices))
    return median, len(prices)


def prune_sold_samples(rule_id: int) -> None:
    """窗口外的成交样本直接删 —— 中位数只看近 30 天，留着只会让表越来越大。"""
    since = config.now() - timedelta(days=get_settings()["median_window_days"])
    execute("DELETE FROM sold_sample WHERE rule_id = %s AND sold_at < %s", (rule_id, since))


# ---------------------------------------------------------------- 每日请求计数

def bump_daily(source: str, requests: int = 0, errors: int = 0) -> None:
    execute("INSERT INTO daily_stat (day, source, requests, errors) VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE requests = requests + VALUES(requests), "
            "errors = errors + VALUES(errors)",
            (config.now().date(), source, requests, errors))


def today_stat() -> dict:
    """今天的【全源合计】请求数 —— 每日上限管的是总量。按源分开看用 today_by_source()。"""
    row = one("SELECT COALESCE(SUM(requests),0) requests, COALESCE(SUM(errors),0) errors "
              "FROM daily_stat WHERE day = %s", (config.now().date(),))
    return row or {"requests": 0, "errors": 0}


def today_by_source() -> list[dict]:
    return query("SELECT source, requests, errors FROM daily_stat WHERE day = %s ORDER BY source",
                 (config.now().date(),))
