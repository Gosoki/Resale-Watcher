"""数据库访问。全部手写 SQL，【每个线程一条长连接】。

【为什么不是每条 SQL 一条短连接】原先是那样，注释里写的理由是「局域网内建连接
约几毫秒」—— 这个数是错的。2026-09-17 实测到 10.0.10.20：建连（TCP + TLS +
caching_sha2 鉴权）31ms，而同一条连接上 SELECT 1 只要 3ms。也就是每条查询
90% 的时间花在握手上，与 SQL 内容无关：命中页 19 条查询 803ms，复用连接后 98ms。
面板上每一处"卡一下"——打开首页 3〜7 秒、点一面旗 1.2 秒、状态栏每 10 秒一顿——
根子都在这里，之前为它打的补丁（_DIRTY 懒重建、marked_ids 一次取整份）
只是绕着走。

【为什么不共享一条】PyMySQL 的连接不是线程安全的，轮询线程和 NiceGUI 的事件
循环共用一条会撞出 "Packet sequence number wrong"。所以是 threading.local：
每个线程各持一条，谁也不碰谁的。常驻的就轮询、看门狗、事件循环三条，
io_bound 的工作线程最多再几条，离 max_connections=151 很远。

【连接会断，要认】MySQL 的 wait_timeout 是 8 小时，io_bound 的工作线程闲置
一夜之后第一条查询必然撞上 "MySQL server has gone away"。所以：
  - 闲置超过 IDLE_PING_SEC 再用之前先 ping 一下，死了就重连（只多一个往返，
    而且只在闲置之后才有）；
  - 执行中撞上连接类错误就把本线程这条丢掉，下一次用时重建。
    【不自动重试】INSERT price_log / watch_rule 不是幂等的，盲目重试会多出一行。
"""
import contextlib
import re
import statistics
import threading
import time
from datetime import timedelta

import logging

import pymysql
from pymysql.cursors import DictCursor

import config

log = logging.getLogger("store")

SCHEMA_PATH = config.BASE_DIR / "db" / "schema.sql"


IDLE_PING_SEC = 60                      # 闲置超过这么久，用之前先 ping
_CONN_ERRORS = (pymysql.err.OperationalError, pymysql.err.InterfaceError)
_local = threading.local()


def _connect():
    # config.DB 里带着 connect/read/write 三个超时 —— 理由见 config.py。
    return pymysql.connect(**config.DB, charset="utf8mb4", cursorclass=DictCursor,
                           autocommit=True)


def _get():
    """本线程的那条连接；没有就建，闲太久就先 ping，死了就重建。"""
    c = getattr(_local, "c", None)
    if c is not None and time.time() - _local.used > IDLE_PING_SEC:
        try:
            c.ping(reconnect=False)      # reconnect=True 在 PyMySQL 2.2 已弃用
        except Exception:                # noqa: BLE001 - 任何异常都当它死了
            _drop_conn()
            c = None
    if c is None:
        c = _local.c = _connect()
    _local.used = time.time()
    return c


def _drop_conn() -> None:
    """丢掉本线程这条连接（出错后、或测试里要换 connect 时用）。"""
    c = getattr(_local, "c", None)
    _local.c = None
    if c is not None:
        try:
            c.close()
        except Exception:                # noqa: BLE001 - 本来就是坏的
            pass


@contextlib.contextmanager
def conn(db: bool = True):
    """db=True：本线程的长连接（别 close）。db=False：一次性短连接，建库用 ——
    它没选库，缓存下来的话后面所有查询都会 No database selected。"""
    if not db:
        kw = dict(config.DB, charset="utf8mb4", cursorclass=DictCursor, autocommit=True)
        kw.pop("database")
        c = pymysql.connect(**kw)
        try:
            yield c
        finally:
            c.close()
        return
    try:
        yield _get()
    except _CONN_ERRORS:
        _drop_conn()
        raise


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
    have: {(表, 列): {"comment": 库里的注释, "type": 库里的类型如 "varchar(24)"}}
    返回 (要新增的, 要改的)，元素都是 (表, 列, 定义)。

    【注释和类型都要比】只比注释的话，把 VARCHAR(24) 改成 VARCHAR(32) 而注释没动，
    对已有的库就毫无作用 —— 而这种"悄悄不生效"正是本函数要消灭的东西。
    实测本库 65 个列的 DDL 类型串和 information_schema 的 COLUMN_TYPE 逐一相等
    （TINYINT(1) 这类也对得上），所以直接比字符串不会天天误报去 ALTER 全表。
    """
    tables = {t for t, _ in have}          # 库里已经存在的表
    adds, mods = [], []
    for (table, col), definition in want.items():
        if table not in tables:
            continue                        # 整张表还没建：CREATE TABLE 会连列带注释一起建出来
        cur = have.get((table, col))
        if cur is None:
            adds.append((table, col, definition))
            continue
        m = re.search(r"COMMENT\s+'((?:[^']|'')*)'", definition)
        comment_differs = bool(m) and m.group(1).replace("''", "'") != cur["comment"]
        t = re.match(r"\w+\s+(\S+)", definition)
        type_differs = bool(t) and t.group(1).lower() != cur["type"]
        if comment_differs or type_differs:
            # 【MODIFY 必须剥掉 inline PRIMARY KEY】app_setting.k、poller_lease.id 这种
            # 定义里带 "PRIMARY KEY" 的列，原样拼进 MODIFY COLUMN 会报
            # "Multiple primary key defined" —— 而这句跑在启动路径上，等于改一句
            # 注释就让进程起不来。主键本身不会变，MODIFY 只管类型和注释。
            mods.append((table, col, re.sub(r"\s+PRIMARY KEY\b", "", definition)))
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

    have = {(r["TABLE_NAME"], r["COLUMN_NAME"]):
            {"comment": r["COLUMN_COMMENT"], "type": (r["COLUMN_TYPE"] or "").lower()}
            for r in query("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT, COLUMN_TYPE "
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
    "exclude_sellers, "
    "price_min, price_max, condition_ids, allow_shops, check_desc, "
    "deal_price, deal_ratio, quick_min, note"
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
    # 【只插 data 里真有的列，别用 data.get() 兜底成 None】
    # 原先是对 RULE_COLUMNS 逐列 data.get(c)：调用方少给一个键就往 NOT NULL 的列里
    # 塞 NULL，STRICT 模式下直接 IntegrityError。每往 watch_rule 加一个新列，
    # tools/seed.py 这种手写字典的调用方就会当场炸掉（exclude_sellers 就是这么炸的一次）。
    # 省掉的列走 DDL 里的 DEFAULT，这也正是 update_rule 一直以来的做法。
    cols = [c.strip() for c in RULE_COLUMNS.split(",") if c.strip() in data]
    if not cols:
        raise ValueError("insert_rule 收到的字典里没有任何 watch_rule 的列")
    ph = ", ".join(["%s"] * len(cols))
    sql = (f"INSERT INTO watch_rule ({', '.join(cols)}, created_at, updated_at) "
           f"VALUES ({ph}, %s, %s)")
    args = [data[c] for c in cols] + [config.now(), config.now()]
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



# ---------------------------------------------------------------- 标记（书签）

def set_marked(row: dict, rule_name: str, on: bool) -> None:
    """标记 / 取消标记。

    【标记的是快照，不是引用】存下此刻的标题、价格、缩略图。商品下架、
    平台删帖、详情页 404 之后，这一页仍然看得到你当初标的是什么、多少钱。
    只存 ID 的话，过几周回来只剩一排死链接。

    【重复标记不覆盖】ON DUPLICATE 里故意什么都不改：快照和时间都保持第一次
    按下的那一刻。否则你在列表里不小心点两下，"我什么时候标的"就变成了现在。
    """
    if not on:
        execute("DELETE FROM marked_item WHERE source = %s AND item_id = %s",
                (row["source"], row["item_id"]))
        return
    execute("INSERT INTO marked_item (source, item_id, name, price, thumb_url, "
            "rule_name, marked_at) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE item_id = item_id",
            (row["source"], row["item_id"], row["name"], row["price"],
             row.get("thumb_url") or "", rule_name[:64], config.now()))


def set_mark_note(source: str, item_id: str, note: str) -> None:
    execute("UPDATE marked_item SET note = %s WHERE source = %s AND item_id = %s",
            (note[:255], source, item_id))


# ---------------------------------------------------------------- 剔除（只影响命中页）

def set_hidden(row: dict, rule_name: str, on: bool) -> None:
    """从命中页剔除 / 恢复。

    【只按 (source, item_id) 记，不带 rule_id】和标记同一个口径：同一个链接被
    两条规则抓到就是两行 item，但它是同一件东西 —— 你说「不想再看到它」，
    指的是这件东西，不是「它在某条规则下的那一行」。

    【存一份标题快照】恢复列表要告诉你剔掉的是什么。只存 ID 的话，哪天那条规则
    被删了（item 跟着一起删），这一页就只剩一排认不出的编号，而你已经不记得
    当初为什么剔掉它了。

    【重复剔除不覆盖时间】ON DUPLICATE 里什么都不改，理由同 set_marked。
    """
    if not on:
        execute("DELETE FROM hidden_item WHERE source = %s AND item_id = %s",
                (row["source"], row["item_id"]))
        return
    execute("INSERT INTO hidden_item (source, item_id, name, rule_name, hidden_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE item_id = item_id",
            (row["source"], row["item_id"], row["name"][:255],
             (rule_name or "")[:64], config.now()))


def hidden_ids() -> set[tuple[str, str]]:
    """已剔除的 (source, item_id) 集合。整份取出来，理由同 marked_ids。"""
    return {(r["source"], r["item_id"])
            for r in query("SELECT source, item_id FROM hidden_item")}


def hidden_items() -> list[dict]:
    """剔除列表，最近剔的在前。"""
    return query("SELECT * FROM hidden_item ORDER BY hidden_at DESC")


def mark_note_of(source: str, item_id: str) -> str:
    """这条标记的备注；没标记过就是空串。取消标记前问一句用。"""
    row = one("SELECT note FROM marked_item WHERE source = %s AND item_id = %s", (source, item_id))
    return (row or {}).get("note") or ""


def marked_ids() -> set[tuple[str, str]]:
    """已标记的 (source, item_id) 集合。

    列表页一次查出来整份，而不是每行查一次 —— 命中页一屏几十行，
    每行一个 SELECT 会让翻页肉眼可见地卡。
    """
    return {(r["source"], r["item_id"])
            for r in query("SELECT source, item_id FROM marked_item")}


def marked_items() -> list[dict]:
    """标记列表，最近标的在前。每条带一个 live 字段＝它现在怎么样了（没有就是 None）。

    【为什么要带现况】快照回答"我当初标的是什么"，现况回答"它后来怎么了"——
    降价了没、卖掉了没。两个都在才用得上：只有快照的话这一页就是一堆旧照片。

    【同一个商品可能有多行 item】被两条规则抓到就是两行。取最近一次看到的那行：
    不同规则的轮次错开，早一轮的那行价格可能是旧的。
    """
    rows = query("SELECT * FROM marked_item ORDER BY marked_at DESC")
    if not rows:
        return []
    ids = list({m["item_id"] for m in rows})
    live: dict[tuple[str, str], dict] = {}
    # 按 last_seen_at 升序扫，后写的覆盖先写的 —— 留下的就是最新那行
    for r in query(f"SELECT source, item_id, price, status, sold_at FROM item "
                   f"WHERE item_id IN ({','.join(['%s'] * len(ids))}) "
                   f"ORDER BY last_seen_at ASC", ids):
        live[(r["source"], r["item_id"])] = r
    for m in rows:
        m["live"] = live.get((m["source"], m["item_id"]))
    return rows


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


# ---------------------------------------------------------------- 轮询租约

def claim_lease(holder: str, ttl_sec: int) -> str:
    """争一次租约，返回争完之后的持有者（等于 holder 就是拿到了）。

    【判断和写入必须在同一条 UPDATE 里】先 SELECT 看到"过期了"再 UPDATE，
    两个实例会同时看到过期、同时写、都以为自己拿到了 —— 那正是这张表要防的事。
    WHERE 里的条件就是判断，行数改了就是拿到了；然后再 SELECT 一次只是为了
    把最终持有者读回来（UPDATE 的 rowcount 在值没变时是 0，不能拿它当结果）。

    【taken_at 必须排在 holder 前面赋值】MySQL 的 SET 从左到右生效，
    后面的表达式看到的是前面刚改过的值 —— holder 先改了，taken_at 里的
    IF(holder = 我) 就恒真，换人的那一刻拿不到新的 taken_at。
    """
    now = config.now()
    until = now + timedelta(seconds=ttl_sec)
    execute("INSERT IGNORE INTO poller_lease (id, holder, taken_at, expires_at) "
            "VALUES (1, %s, %s, %s)", (holder, now, until))
    execute("UPDATE poller_lease "
            "SET taken_at = IF(holder = %s, taken_at, %s), holder = %s, expires_at = %s "
            "WHERE id = 1 AND (holder = %s OR expires_at < %s)",
            (holder, now, holder, until, holder, now))
    return one("SELECT holder FROM poller_lease WHERE id = 1")["holder"]


def renew_lease(holder: str, ttl_sec: int) -> None:
    """续期。只在自己还是持有者时才延长；不是了就什么都不做（别抢）。一条 SQL。"""
    now = config.now()
    execute("UPDATE poller_lease SET expires_at = %s WHERE id = 1 AND holder = %s",
            (now + timedelta(seconds=ttl_sec), holder))


def release_lease(holder: str) -> None:
    """主动放掉租约（只放自己的）。

    【为什么要主动放】不放的话 ./run.sh restart 之后新进程会看到旧进程的租约还没到期，
    老老实实待命到 TTL 用完 —— 重启一次白等 10 分钟。进程正常退出时放掉，
    新进程第一轮就能接上；被 kill -9 的话放不了，那就还是靠 TTL 到期。
    """
    # 【删行，不是把 expires_at 设成现在】claim 的条件是 expires_at < now，
    # 同一秒内新进程来争会因为"等于而不是小于"拿不到，白等一轮。删掉行最干净：
    # claim 开头的 INSERT IGNORE 会重建它，谁先到谁拿。
    execute("DELETE FROM poller_lease WHERE id = 1 AND holder = %s", (holder,))


def lease_holder() -> str | None:
    row = one("SELECT holder, expires_at FROM poller_lease WHERE id = 1")
    if not row or row["expires_at"] < config.now():
        return None
    return row["holder"]


# ---------------------------------------------------------------- 面板整页一次取回
# 【为什么要这一组】面板的每一页原先都是「每条规则一条 SQL、每个源再一条」——
# 规则页 33 条、命中页 19 条、成交页 14 条。库在远程，每条 SQL 一次往返，
# 页面的时间几乎全花在往返上。这里每张表整份取回，页面在 Python 里按规则分。
# 【缺行要给默认值】rule_state / rule_source_state 是 poller 第一次扫到才补的，
# 新建一条规则、还没扫过就打开页面，字典里就是没有它 —— 渲染处不许直接下标。
# 【别拿这些替掉 get_state / get_source_state】那两个带 INSERT IGNORE 副作用，
# poller 的 update_* 靠它们保证行存在；这里只是面板读，不该顺手去写。
STATE_DEFAULT = {"median_price": None, "sample_count": 0}
SOURCE_STATE_DEFAULT = {"last_scan_at": None, "last_sold_at": None, "last_total": 0,
                        "truncated": 0, "last_error": ""}


def rule_states() -> dict[int, dict]:
    return {r["rule_id"]: r for r in query("SELECT * FROM rule_state")}


def source_states() -> dict[tuple[int, str], dict]:
    return {(r["rule_id"], r["source"]): r for r in query("SELECT * FROM rule_source_state")}


def item_counts() -> dict[tuple[int, str], dict]:
    """每个 (规则, 源) 的入库数和在售命中数。SUM 回来的是 Decimal，这里就转成 int。"""
    return {(r["rule_id"], r["source"]): {"t": int(r["t"] or 0), "h": int(r["h"] or 0)}
            for r in query("SELECT rule_id, source, COUNT(*) t, "
                           "SUM(matched = 1 AND status = 'on_sale') h "
                           "FROM item GROUP BY rule_id, source")}


def first_seen_by_rule() -> dict[int, object]:
    """每条规则库里最早的 first_seen_at —— 命中页判「冷启动」用。"""
    return {r["rule_id"]: r["m"] for r in
            query("SELECT rule_id, MIN(first_seen_at) m FROM item GROUP BY rule_id")}


def live_matched() -> list[dict]:
    """全部在售命中，按规则分块、块内按「市价的百分比」升序 —— 命中页规则组直接用。
    捡漏汇总要跨规则重排，调用方自己 sort。"""
    return query("SELECT * FROM item WHERE matched = 1 AND status = 'on_sale' "
                 "ORDER BY rule_id, COALESCE(deal_pct, 999), price")


def sold_tracked_all() -> dict[int, list[dict]]:
    """跟到成交的（matched=1 且 sold_out），按规则分。"""
    out: dict[int, list[dict]] = {}
    for r in query("SELECT * FROM item WHERE status = 'sold_out' AND matched = 1 "
                   "ORDER BY rule_id, sold_at DESC"):
        out.setdefault(r["rule_id"], []).append(r)
    return out


def sold_samples_recent(per_rule: int = 80) -> dict[int, list[dict]]:
    """每条规则最近 per_rule 条成交样本，一条 SQL（窗口函数，MySQL 8+）。"""
    out: dict[int, list[dict]] = {}
    for r in query("SELECT rule_id, source, price, sold_at, sample_kind FROM ("
                   "  SELECT rule_id, source, price, sold_at, sample_kind, "
                   "         ROW_NUMBER() OVER (PARTITION BY rule_id ORDER BY sold_at DESC) rn "
                   "  FROM sold_sample) t WHERE rn <= %s ORDER BY rule_id, sold_at DESC",
                   (per_rule,)):
        out.setdefault(r["rule_id"], []).append(r)
    return out


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


def save_detail(source: str, item_id: str, rule_id: int, description: str, desc_warn: str,
                ship_from: str = "") -> None:
    """存描述 + 警示标签 + 发货地。【不动 matched】描述只提示，不参与合不合适的判定。

    【ship_from 只有这里能写】三个源都只在详情响应里给发货地，搜索结果里没有 ——
    所以没拉过详情的商品这一列恒为空，面板上按「未知」处理、不打地区标签。
    """
    execute("UPDATE item SET description = %s, desc_checked = 1, desc_warn = %s, ship_from = %s "
            "WHERE source = %s AND item_id = %s AND rule_id = %s",
            (description, desc_warn, ship_from, source, item_id, rule_id))


def set_deal(source: str, item_id: str, rule_id: int, is_deal: int, deal_pct: int | None) -> None:
    execute("UPDATE item SET is_deal = %s, deal_pct = %s "
            "WHERE source = %s AND item_id = %s AND rule_id = %s",
            (is_deal, deal_pct, source, item_id, rule_id))


# 终态：到了这两个状态，商品不会再变了
TERMINAL = ("sold_out", "gone")


def set_status(source: str, item_id: str, rule_id: int, status: str, sold_at=None,
               price=None) -> None:
    """改状态。

    【进终态不摘追踪】卖掉/下架的商品会【留在追踪页上】。你盯一件东西盯了几天，
    结果它一成交就从你的列表里消失 —— 最该看的那一刻（最后卖了多少、被谁抬上去的）
    反而没了。追踪页就是你自己挑出来的那几件，它们的结局属于那一页。
    不刷新它们是另一回事：tracked_due 只挑 on_sale/trading，终态的一个请求都不会再发，
    所以留着不花任何代价。
    """
    # 【读到价就一起写回】对账拉详情确认售出时，接口给的是最终成交价；不写回的话
    # item.price 停在它在售时的旧值，成交页上的价格/百分比/「曾是捡漏」全是旧的。
    sets, args = ["status = %s"], [status]
    if sold_at is not None:
        sets.append("sold_at = %s"); args.append(sold_at)
    if price:
        sets.append("price = %s"); args.append(int(price))
    execute(f"UPDATE item SET {', '.join(sets)} "
            "WHERE source = %s AND item_id = %s AND rule_id = %s",
            (*args, source, item_id, rule_id))


def set_tracked(source: str, item_id: str, rule_id: int, on: bool) -> None:
    execute("UPDATE item SET tracked_at = %s "
            "WHERE source = %s AND item_id = %s AND rule_id = %s",
            (config.now() if on else None, source, item_id, rule_id))


# 还在动的商品：只有这两种状态才值得再发请求刷新
LIVE = ("on_sale", "trading")


def tracked_items(rule_id: int | None = None) -> list[dict]:
    """追踪中的商品。还在售的排前面，已经卖掉/下架的沉到底，各自按最近追的在前。

    【终态的要沉底，不能按 tracked_at 混排】卖掉的会一直留着（你盯的东西的结局
    属于这一页），日子久了就比在售的多。不沉底的话，真正还能出手的那几件会被
    埋在一堆已成交里 —— 而这一页存在的理由就是"还能动手的那几件"。
    """
    order = ("ORDER BY status IN ('sold_out', 'gone'), tracked_at DESC")
    if rule_id is None:
        return query(f"SELECT * FROM item WHERE tracked_at IS NOT NULL {order}")
    return query(f"SELECT * FROM item WHERE tracked_at IS NOT NULL AND rule_id = %s {order}",
                 (rule_id,))


def tracked_due(track_min: int, limit: int, trading_min: int | None = None) -> list[dict]:
    """该刷新的追踪商品。

    【用 last_seen_at 判到点，不另开一列】它的语义是「最后一次拿到这件商品的新数据」，
    而整轮扫描和单独拉详情都算「拿到新数据」。这样刚被整轮扫过的商品不会立刻再被
    单独拉一次 —— 搜索结果里已经有价格和状态了，再发一个请求纯属浪费。

    【最久没刷新的优先】追的件数超过 limit 时轮着来，不会有谁被永久饿死。
    已经卖掉/下架的不再刷新：状态是终态了，再拉也不会变 —— 但它们【仍然留在
    追踪页上】，只是不花请求。
    """
    # 【交易中的按 trading_min 算】它的详情几天都不会变，按 track_min 每 5 分钟拉一次是白烧
    now = config.now()
    return query("SELECT source, item_id, rule_id, price, name, matched FROM item "
                 f"WHERE tracked_at IS NOT NULL AND status IN {LIVE} "
                 "AND last_seen_at < CASE WHEN status = 'trading' THEN %s ELSE %s END "
                 "ORDER BY last_seen_at ASC LIMIT %s",
                 (now - timedelta(minutes=trading_min or track_min),
                  now - timedelta(minutes=track_min), limit))


def update_tracked(source: str, item_id: str, rule_id: int, d: dict, old_price: int) -> None:
    """把追踪刷新拿到的详情写回去：价格、状态、出价数、发货地。

    【状态为空串时不动 status】详情页解析不出状态是常有的事（平台改版、异常页），
    猜一个会把还在售的商品从列表里抹掉 —— 和售出对账那边遵守同一条规矩。
    【价格变了才写 price_log】每次都写会让那张表毫无信息量地暴涨。
    """
    now = config.now()
    price = int(d.get("price") or 0)
    sets, args = [], []
    if price > 0:
        sets += ["price = %s", "min_price = LEAST(min_price, %s)"]
        args += [price, price]
    if d.get("status"):
        sets.append("status = %s"); args.append(d["status"])
        if d["status"] == "sold_out":
            sets.append("sold_at = %s"); args.append(now)
    if d.get("bid_count") is not None:
        sets.append("bid_count = %s"); args.append(d["bid_count"])
    if d.get("ship_from"):
        sets.append("ship_from = %s"); args.append(d["ship_from"])
    if not sets:
        return
    execute(f"UPDATE item SET {', '.join(sets)} "
            f"WHERE source = %s AND item_id = %s AND rule_id = %s",
            args + [source, item_id, rule_id])
    if price > 0 and price != old_price:
        add_price_log(source, item_id, price, now)


# 推送正文要用到的列。【两条待推查询必须取一模一样的列】各写一份的话，
# 加一列只加在一边，另一边推出去的消息就悄悄少一块 —— 不报错、不进日志。
#   thumb_url 缺 → 每条都走兜底，图永远出不来（Slack 对空 image_url 回 400）
#   end_time  缺 → 拍卖推送里没有截止时间，而那正是拍卖最要紧的一个数
NOTIFY_COLS = ("source, item_id, rule_id, name, price, is_deal, deal_pct, bid_count, "
               "end_time, thumb_url, notified_at, notified_price")
# 【剔除的不推】用户剔掉一件，意思就是"别再拿它烦我"——命中页不显示了、手机上却
# 还在响，会被当成 bug。两条待推查询都带上这一句。
NOT_HIDDEN = ("AND NOT EXISTS (SELECT 1 FROM hidden_item h "
              "WHERE h.source = item.source AND h.item_id = item.item_id) ")


def pending_notify(rule_id: int, only_deal: bool, redrop_pct: int = 0) -> list[dict]:
    """该推的在售命中商品：还没推过的，以及（开了 redrop_pct 时）推过之后又跌够了的。

    【条件里的 status = 'on_sale' 不能省】商品卖掉之后 matched 仍然是 1
    （它当时确实合适），推一条"快看这个好货"过去而人点进去是已售出，
    比不推还差。

    【再跌 N% 重推】原先一件商品终身只推一次 —— 库里 26 条已推里有 5 条推完又降过价，
    你一条都不知道。比的是「上次推送时的价格」（notified_price），不是首见价：
    每推一次基线刷新一次，掉几百块不会反复轰炸。N=0 关闭，也就是老行为。
    """
    sql = (f"SELECT {NOTIFY_COLS} FROM item WHERE rule_id = %s AND matched = 1 "
           "AND status = 'on_sale' " + NOT_HIDDEN +
           "AND (notified_at IS NULL OR (%s > 0 AND notified_price IS NOT NULL "
           "     AND price * 100 <= notified_price * (100 - %s)))")
    if only_deal:
        sql += " AND is_deal = 1"
    # 和面板同一个排序：最划算的排最前，万一撞上限被整批跳过也是先看到好的
    return query(sql + " ORDER BY COALESCE(deal_pct, 999), price",
                 (rule_id, redrop_pct, redrop_pct))

def pending_final(rule_id: int, minutes: int) -> list[dict]:
    """快到点的拍卖里，价格还在捡漏线内、而且还没提醒过的那些。

    【为什么要第二条】第一条是它刚变成捡漏那一刻发的 —— 那时可能还剩两天。
    两天足够你把它忘干净，而拍卖到点就没了，没有第二次机会。

    【只挑拍卖】bid_count IS NOT NULL 就是"这是拍卖"。普通商品没有到点一说，
    晚一天去看它还在那儿。

    【is_deal = 1 是硬条件，不看 notify_on】被别人抬出捡漏线的就不该再提醒 ——
    那时它只是一件"你原本看得上的贵东西"，催你去看等于催你冲动出价。

    【notified_at 必须早于进入这个窗口的那一刻】少了最后这一句，一件在最后
    半小时里才第一次变成捡漏的商品会被推两遍：先是第一条（它刚够格），
    几分钟后又来一条"快结束"—— 你看到的是同一件东西，只隔了五分钟。
    """
    now = config.now()
    return query(
        f"SELECT {NOTIFY_COLS} FROM item "
        "WHERE rule_id = %s AND matched = 1 AND status = 'on_sale' AND is_deal = 1 "
        "AND bid_count IS NOT NULL AND end_time IS NOT NULL "
        "AND end_time > %s AND end_time <= %s " + NOT_HIDDEN +
        "AND final_notified_at IS NULL "
        "AND notified_at IS NOT NULL "
        "AND notified_at < DATE_SUB(end_time, INTERVAL %s MINUTE) "
        "ORDER BY end_time",
        (rule_id, now, now + timedelta(minutes=minutes), minutes))


def _mark_pushed(rows: list[dict], col: str, extra_set: str = "") -> None:
    """把这批商品的某个推送时间戳戳到现在。

    推送成功与否都要标 —— 见 core/notify.py 里「失败也标已推」的说明。
    col / extra_set 是本文件写死的 SQL 片段，不来自外部输入。
    """
    if not rows:
        return
    keys = [(r["source"], r["item_id"], r["rule_id"]) for r in rows]
    holes = ",".join(["(%s,%s,%s)"] * len(keys))
    execute(f"UPDATE item SET {col} = %s{extra_set} "
            f"WHERE (source, item_id, rule_id) IN ({holes})",
            (config.now(), *[v for k in keys for v in k]))


def mark_notified(rows: list[dict]) -> None:
    """标成「已推」，顺手记下此刻的价格 —— 「再跌 N% 重推」比的就是它。"""
    _mark_pushed(rows, "notified_at", ", notified_price = price")

def mark_final_notified(rows: list[dict]) -> None:
    """标成「快结束提醒已推」。这一条每件商品同样只发一次。"""
    _mark_pushed(rows, "final_notified_at")


def live_ids_among(rule_id: int, source: str, ids: list[str]) -> list[str]:
    """这批 ID 里，库里还标着在售/交易中的有哪些。一条 SQL，给成交轮改状态用。"""
    if not ids:
        return []
    holes = ",".join(["%s"] * len(ids))
    return [r["item_id"] for r in query(
        f"SELECT item_id FROM item WHERE rule_id = %s AND source = %s "
        f"AND status IN {LIVE} AND item_id IN ({holes})", (rule_id, source, *ids))]


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


def last_change(log: list[dict], current: int) -> tuple[int, object] | None:
    """从一件商品的价格历史里取出「上一次的价格」和「什么时候变成现价的」。

    log 是这件商品的全部 price_log，按时间升序；current 是它此刻的价格。
    从没变过价就返回 None。时间取不到时返回 (上次价格, None)。

    【必须跳过末尾连着的同价记录，不能直接拿倒数第二条】库里有 27 条同价记录
    是 add_price_log 加上去重【之前】留下的（全在 2026-09-12 17:36〜18:53 那一段）：
    price_log 不带 rule_id，那阵子同一个链接被两条规则命中就各记一条。
    直接取倒数第二条的话，这 26 件商品的「上次价格」会和现价一模一样，
    页面上就是「上次 ¥2,650 · 现在 ¥2,650」，看着像程序坏了。
    拿 current 往回找第一个不同的价，这种历史脏数据和以后可能出现的
    别的重复写入就都绕开了。
    """
    i = len(log)
    while i > 0 and log[i - 1]["price"] == current:
        i -= 1
    if i == 0:                      # 历史里从头到尾只有现在这一个价
        return None
    # log[i] 是第一次记到现价的那条；i == len(log) 说明历史里根本没记过现价
    return log[i - 1]["price"], (log[i]["noted_at"] if i < len(log) else None)


def prev_prices(rows: list[dict]) -> dict[tuple[str, str], tuple]:
    """一批商品各自的「上一次的价格」。键是 (source, item_id)，没变过价的不进字典。

    【一次查完，不要每行查一次】命中页一屏几十行，理由同 marked_ids。
    """
    cur = {(r["source"], r["item_id"]): r["price"] for r in rows}
    if not cur:
        return {}
    keys = sorted(cur)
    holes = ",".join(["(%s,%s)"] * len(keys))
    log: dict[tuple[str, str], list] = {}
    for r in query(f"SELECT source, item_id, price, noted_at FROM price_log "
                   f"WHERE (source, item_id) IN ({holes}) ORDER BY noted_at, id",
                   tuple(v for k in keys for v in k)):
        log.setdefault((r["source"], r["item_id"]), []).append(r)
    out = {}
    for k, v in log.items():
        ch = last_change(v, cur[k])
        if ch:
            out[k] = ch
    return out


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
