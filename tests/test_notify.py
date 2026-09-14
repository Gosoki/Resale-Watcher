"""推送。

【为什么这条路径值得测】它的两种失败都很难在日志里看出来：
  发不出去 —— JSON 模板被商品标题里的引号撑破，对方回 400，
              日志里只有一个没头没尾的状态码
  发太多了 —— 刚填上 notify_url 那一刻库里几十件老命中一起轰出去，
              人被淹一次就再也不开推送了
下面这几条锁的就是这两件事，外加「推送坏了不能拖垮抓取」。
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import notify  # noqa: E402

RULE = {"id": 1, "name": "RTX 5090 单卡"}


def item(**kw):
    base = {"source": "mercari", "item_id": "m1", "rule_id": 1, "name": "RTX 5090",
            "price": 700000, "is_deal": 1, "deal_pct": 85, "bid_count": None,
            "thumb_url": "https://cdn/thumb.jpg"}
    return {**base, **kw}


class FakeStore:
    """只实现 notify 真正用到的那几个方法。"""

    def __init__(self, settings, rows, final=()):
        self.settings, self.rows, self.final = settings, rows, list(final)
        self.marked = []
        self.marked_final = []
        self.final_asked = []            # pending_final 被问过几次、用的什么窗口

    def get_settings(self):
        return self.settings

    def pending_notify(self, rule_id, only_deal):
        return [r for r in self.rows if r["is_deal"]] if only_deal else list(self.rows)

    def pending_final(self, rule_id, minutes):
        self.final_asked.append(minutes)
        return list(self.final)

    def mark_notified(self, rows):
        self.marked.extend(rows)

    def mark_final_notified(self, rows):
        self.marked_final.extend(rows)

    def get_state(self, rule_id):
        return {"median_price": 820000}


# 【每次 run() 都把真 sleep 换掉】push_new 两条之间要等 1.2 秒（Slack 限 1 条/秒）。
# 不换的话光"推 5 条"那个用例就要跑 5 秒 —— 实测整套测试从 0.56s 涨到 9.02s，
# 而慢测试的下场是被人加 skip。换成记录器，顺便拿它断言间隔够不够。
SLEPT: list[float] = []


class _NoSleep:
    @staticmethod
    def sleep(seconds):
        SLEPT.append(seconds)


def run(monkeypatch_target, settings, rows, sender=None, final=()):
    """装好假的 store 和假的 post，跑一次 push_new，返回 (发出去的内容, FakeStore)。"""
    sent = []
    fake = FakeStore(settings, rows, final)
    SLEPT.clear()
    monkeypatch_target["store"], monkeypatch_target["post"] = notify.store, notify.post
    saved_time = notify.time
    notify.store = fake
    notify.post = sender or (lambda url, tpl, text, thumb="": sent.append((url, tpl, text)))
    notify.time = _NoSleep
    try:
        n = notify.push_new(RULE)
    finally:
        notify.store, notify.post = monkeypatch_target["store"], monkeypatch_target["post"]
        notify.time = saved_time
    return sent, fake, n


# ---------------------------------------------------------------- JSON 转义

def test_标题里的双引号不会撑破_JSON_模板():
    """日文商品名里带引号并不罕见。直接字符串替换的话对方回 400，
    而日志里只看得到一个状态码，排查方向完全没有线索。"""
    out = notify.render('{"content": "{text}"}', '新品 "未開封" RTX5090')
    import json
    assert json.loads(out)["content"] == '新品 "未開封" RTX5090'


def test_反斜杠和换行也要转义():
    import json
    out = notify.render('{"content": "{text}"}', "第一行\n带反斜杠 C:\\temp")
    assert json.loads(out)["content"] == "第一行\n带反斜杠 C:\\temp"


# ---------------------------------------------------------------- 正文

def test_正文把最要紧的信息放前两行():
    """手机通知栏只看得见前两行。"""
    lines = notify.compose(RULE, item(), 820000).splitlines()
    assert lines[0] == "🟢 捡漏 | RTX 5090 单卡"
    assert lines[1] == "RTX 5090"
    assert "¥700,000" in lines[2] and "85%" in lines[2]
    assert lines[-1].startswith("https://jp.mercari.com/item/")


def test_拍卖必须带竞价提示():
    """拍卖的「当前价」只在此刻成立。不写这句，推送就是在误导人。"""
    text = notify.compose(RULE, item(source="yahoo_auction", item_id="z1", bid_count=7), None)
    assert "已 7 次出价" in text and "还会涨" in text


def test_拍卖要同时给剩余时间和绝对截止时间():
    """【两个都得有】推送是躺在通知栏里的静止消息，面板是打开就重算的。
    只写"剩 3 小时"的话，你半夜翻到这条时它可能早就结束了，而消息还写着剩 3 小时；
    只写绝对时间又得自己心算 —— 而拍卖要的就是扫一眼的紧迫感。
    """
    end = config.now() + timedelta(hours=3, minutes=5)
    text = notify.compose(
        RULE, item(source="yahoo_auction", item_id="z1", bid_count=7, end_time=end), None)
    assert "剩 3 小时" in text
    assert f"{end:%m-%d %H:%M} 截止" in text


def test_拍卖没给截止时间时不留半截括号():
    """【源不给 end_time 是会发生的】ヤフオク 的搜索结果就不一定带。
    直接往正文里拼的话会推出一句「（ 截止）」，看着像程序坏了。
    """
    text = notify.compose(
        RULE, item(source="yahoo_auction", item_id="z1", bid_count=7, end_time=None), None)
    assert "截止" not in text and "剩" not in text
    assert "已 7 次出价" in text


def test_非拍卖商品不写截止时间():
    """普通商品没有"到点就没了"这回事，多一行只会稀释前两行。"""
    assert "截止" not in notify.compose(RULE, item(), 820000)


# ---------------------------------------------------------------- 开关与阈值

def test_URL_留空时一个请求都不发():
    sent, fake, n = run({}, {"notify_url": "", "notify_on": "deal"}, [item()])
    assert (sent, fake.marked, n) == ([], [], 0)


def test_只推捡漏时命中但不是捡漏的不推():
    sent, _, n = run({}, {"notify_url": "u", "notify_on": "deal", "notify_max_per_round": 5},
                     [item(item_id="m1", is_deal=0), item(item_id="m2", is_deal=1)])
    assert n == 1 and len(sent) == 1
    assert "m2" in sent[0][2]


def test_notify_on_填_matched_时所有命中都推():
    sent, _, n = run({}, {"notify_url": "u", "notify_on": "matched", "notify_max_per_round": 5},
                     [item(item_id="m1", is_deal=0), item(item_id="m2", is_deal=1)])
    assert n == 2 and len(sent) == 2


def test_积压超过上限时一条都不推但全部标为已推():
    """挡的是"刚填上 URL / 刚重启 / 刚放宽规则"这三种一次性井喷。
    标已推是关键 —— 不标的话下一轮又是同样一堆，永远推不出去也永远刷日志。"""
    rows = [item(item_id=f"m{i}") for i in range(9)]
    sent, fake, n = run({}, {"notify_url": "u", "notify_on": "deal",
                             "notify_max_per_round": 5}, rows)
    assert n == 0 and sent == []
    assert len(fake.marked) == 9


def test_恰好等于上限时正常推():
    rows = [item(item_id=f"m{i}") for i in range(5)]
    sent, fake, n = run({}, {"notify_url": "u", "notify_on": "deal",
                             "notify_max_per_round": 5}, rows)
    assert n == 5 and len(fake.marked) == 5


# ---------------------------------------------------------------- 故障

def test_发送失败也标为已推_不无限重试():
    """一条迟到一小时的提醒没有意义，而对着挂掉的地址每轮重试会一直拖慢抓取。"""
    def boom(url, tpl, text, thumb=""):
        raise RuntimeError("connection refused")

    sent, fake, n = run({}, {"notify_url": "u", "notify_on": "deal",
                             "notify_max_per_round": 5}, [item()], sender=boom)
    assert n == 0                      # 一条都没发成功
    assert len(fake.marked) == 1       # 但已经标掉了，下轮不会再试


def test_单条发送失败不影响后面几条():
    calls = []

    def flaky(url, tpl, text, thumb=""):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("第一条挂了")

    rows = [item(item_id=f"m{i}") for i in range(3)]
    _, fake, n = run({}, {"notify_url": "u", "notify_on": "deal",
                          "notify_max_per_round": 5}, rows, sender=flaky)
    assert len(calls) == 3 and n == 2


# ---------------------------------------------------------------- 接线

def test_推送必须接在常驻轮询的路径上():
    """【这条是踩出来的】推送一开始只挂在 run_once 上，而 run_once 只被面板的
    「立即跑一次」和 ./run.sh once 调用 —— 常驻轮询走的是 loop → _run_round。
    于是正常部署方式（./run.sh start / systemd）下推送一条都发不出去，
    而日志、面板、rule_source_state.last_error 全都正常，没有任何线索。

    更坏的一层：后台轮询期间待推的商品一直堆着，等哪天手点一次「立即跑一次」，
    条数几乎必然超过 notify_max_per_round，被整批标成已推、永久丢掉。
    """
    import inspect
    from core import poller

    assert "push_new" in inspect.getsource(poller.finalize), \
        "finalize 里没有推送 —— 两条路径就都推不出去了"
    for fn, name in [(poller.run_once, "run_once"), (poller._run_round, "_run_round")]:
        assert "finalize(" in inspect.getsource(fn), \
            f"{name} 没有调 finalize —— 这条路径上的重判/捡漏/推送全都不会发生"


# ---------------------------------------------------------------- Slack

SLACK_TPL = '{"text": "{text}"}'
# 真正在用的那个（带图）
SLACK_IMG_TPL = ('{"text": "{text}", "blocks": [{"type": "section", "text": '
                 '{"type": "mrkdwn", "text": "{text}"}, "accessory": {"type": "image", '
                 '"image_url": "{thumb}", "alt_text": "商品图"}}]}')


def test_slack模板产出合法JSON():
    """Slack 的 incoming webhook 只认 {"text": "..."}。
    正文里有换行（我们的提醒本来就是 4 行）和日文引号，转义错一个 Slack 就回 400，
    而 post() 的失败是静默的 —— 你只会觉得"最近没捡漏"。"""
    import json

    text = ('🟢 捡漏 | RTX 5090 单卡\n新品"未開封" \\ 特価\n'
            '¥850,000（市价的 106%）\nhttps://jp.mercari.com/item/m1')
    body = notify.render(SLACK_TPL, text)
    assert json.loads(body)["text"] == text, "转义之后必须还原成一模一样的正文"


def test_slack模板不会被标题里的引号撑破():
    """这是 render() 存在的全部理由：日文商品名里「"未開封"」很常见。"""
    import json

    body = notify.render(SLACK_TPL, '【新品】"未開封" RTX5090')
    json.loads(body)          # 解析不了就会抛，测试直接红


def test_商品链接留在正文最后一行():
    """Slack 靠它自动展开带图的预览卡片。挪到中间就不展开了。"""
    row = item(name="RTX 5090")
    text = notify.compose(RULE, row, 820000)
    assert text.rstrip().splitlines()[-1].startswith("http"), \
        "链接必须是最后一行，否则 Slack 不展开预览"


def test_多条推送之间要留够间隔():
    """【Slack 对每个 webhook 限 1 条/秒】超了回 429，而本模块【失败不重试】——
    一轮推 5 条、不停顿连发的话，后面几条直接丢，日志里只有一行 warning，
    而你丢掉的正是"该立刻去看"的那几件。这两条规矩撞在一起才是真问题，
    单看任何一条都不像 bug。"""
    rows = [item(item_id=f"m{i}") for i in range(4)]
    sent, _, n = run({}, {"notify_url": "u", "notify_on": "deal",
                          "notify_max_per_round": 5}, rows)
    assert n == 4
    assert len(SLEPT) == 3, f"4 条之间该等 3 次（第一条不用等），实际等了 {len(SLEPT)} 次"
    assert all(x >= 1.0 for x in SLEPT), f"间隔不能低于 1 秒，实际 {SLEPT}"


def test_只推一条时不白等():
    """只有一条时多睡 1.2 秒纯属浪费 —— 稳态下一轮多半就 0〜1 条。"""
    sent, _, n = run({}, {"notify_url": "u", "notify_on": "deal",
                          "notify_max_per_round": 5}, [item()])
    assert n == 1 and SLEPT == []


# ---------------------------------------------------------------- 缩略图

def test_带图模板把缩略图地址填进去():
    import json

    body = notify.render(SLACK_IMG_TPL, "标题", "https://cdn/x.jpg")
    d = json.loads(body)
    assert d["blocks"][0]["accessory"]["image_url"] == "https://cdn/x.jpg"
    assert d["text"] == "标题", "顶层 text 不能丢 —— 它是手机通知栏显示的内容"


def test_没有缩略图时整个换成纯文本模板():
    """【Slack 对空 image_url 回 400 invalid_blocks】只把 {thumb} 填成空串的话，
    发出去的是 "image_url": ""，整条消息失败 —— 而本模块失败不重试，那件捡漏就丢了。
    实测过：空串确实是 400，正常地址是 200。"""
    import json

    body = notify.render(SLACK_IMG_TPL, "标题", "")
    d = json.loads(body)
    assert "blocks" not in d, "没图时必须退回纯文本，不能发一个空 image_url 出去"
    assert d["text"] == "标题"


def test_缩略图地址也要转义():
    """地址里可能带引号或反斜杠（源站的 URL 什么都有），不转义会把 JSON 撑破。"""
    import json

    json.loads(notify.render(SLACK_IMG_TPL, "t", 'https://cdn/a"b\\c.jpg'))


def test_不带thumb的模板照旧工作():
    """ntfy/Bark/Discord 那些模板里没有 {thumb}，不能因为加了这个功能就退回兜底。"""
    import json

    body = notify.render(SLACK_TPL, "标题", "")
    assert json.loads(body)["text"] == "标题"


def test_推送时把商品的缩略图传下去():
    """pending_notify 取了 thumb_url，push_new 必须真的把它交给 post ——
    漏传的话每条都会走兜底，图永远出不来，而且不报错。"""
    got = []
    rows = [item(thumb_url="https://cdn/y.jpg")]
    run({}, {"notify_url": "u", "notify_on": "deal", "notify_max_per_round": 5}, rows,
        sender=lambda url, tpl, text, thumb="": got.append(thumb))
    assert got == ["https://cdn/y.jpg"]


def capture(fn, *args):
    """把 store 真正发给驱动的 (SQL, 参数) 截下来。不连库。

    【为什么不用扫源码那一套】原先这几条守卫是 inspect.getsource + 查子串。
    那种守卫对【参数顺序写反】和【timedelta 单位写错】天生看不见 ——
    源码里每个字符串都还在，守卫全绿，而功能已经死透了。
    而且剥注释只剥得掉 # 开头的行，剥不掉 docstring：pending_final 的
    docstring 里原样写着好几个待查的条件名，哪天有人在那段话里提一句 SELECT，
    切点就落进 docstring，守卫立刻变成它自己警告过的那种恒真断言。
    截真 SQL 没有这两个问题：注释和 docstring 根本不在里面。
    """
    from db import store

    got = {}
    saved_q, saved_e = store.query, store.execute
    store.query = lambda sql, params=None: got.update(sql=sql, params=params) or []
    store.execute = lambda sql, params=None: got.update(sql=sql, params=params)
    try:
        fn(*args)
    finally:
        store.query, store.execute = saved_q, saved_e
    return got["sql"], got["params"]


def test_两条待推查询都要带上正文要用的列():
    """【上面那些用例是假 store，盯不住真 SQL】少 SELECT 一列，push_new 拿到的
    row 里就没有那个键，正文悄悄少一块 —— 不报错、不进日志：
      thumb_url 缺 → 每条都走兜底，图永远出不来
      end_time  缺 → 拍卖推送里没有截止时间，而那是拍卖最要紧的一个数
    同类的坑栽过一次：revalidate 漏了 seller_id，导致拉黑在同一轮里被自己撤销。

    【两条查询都要查】它们喂的是同一个 compose。只在一边加列的话，另一批推送
    静默少一块 —— 而两条消息长得几乎一样，你只会觉得"有时候有图有时候没有"。
    """
    from db import store

    for fn, args in [(store.pending_notify, (7, True)), (store.pending_final, (7, 60))]:
        sql, _ = capture(fn, *args)
        assert "thumb_url" in sql, f"{fn.__name__} 少了 thumb_url，推送里的图会永远缺席"
        assert "end_time" in sql, f"{fn.__name__} 少了 end_time，拍卖推送里没有截止时间"


def test_快结束提醒的SQL必须挡住这些():
    """这条查询的每个条件都对应一种"发错了"，而推出去了是撤不回来的。"""
    from db import store

    sql, _ = capture(store.pending_final, 7, 60)
    for cond, why in [
        ("matched = 1", "会推已经被规则判掉的旧货"),
        ("status = 'on_sale'", "卖掉的也会提醒"),
        ("is_deal = 1", "会提醒已经被抬出捡漏线的拍卖，等于催你冲动出价"),
        ("bid_count IS NOT NULL", "会把没有截止时间的普通商品也算进来"),
        ("end_time > %s", "已经结束的也会提醒"),
        ("end_time <= %s", "窗口没有上界，还剩三天的也会立刻收到「快结束」，"
                           "而且顺手把 final_notified_at 烧掉，真到点时反而不推"),
        ("final_notified_at IS NULL", "同一件会每轮提醒一次，直到它结束"),
        ("notified_at IS NOT NULL", "没推过第一条的会直接收到「快结束」"),
        ("DATE_SUB(end_time", "最后半小时才变成捡漏的会被连推两条，只隔几分钟"),
    ]:
        assert cond in sql, f"pending_final 少了 {cond} —— {why}"


def test_快结束窗口的三个时间量必须算对():
    """【这是假 store 最大的一块盲区】上面那些用例里 pending_final 是手喂的
    假数据，minutes 传下去之后从不参与任何计算 —— 而这个功能的全部语义就是
    now / end_time / minutes 三者的关系。它们写错时源码里每个字符串都还在：

      两个边界写反      → end_time > 未来 AND end_time <= 现在，恒空。
                          一条都发不出去，不报错、不进日志，和"最近没有快结束
                          的捡漏"长得一模一样，你永远不会发现。
      单位写成 hours    → 窗口大 60 倍，两天半内结束的全被当成快结束推掉，
                          还顺手把 final_notified_at 烧掉，真到点时反而不推。
    """
    from db import store

    before = config.now()
    sql, params = capture(store.pending_final, 7, 60)
    after = config.now()

    rid, lo, hi, mins = params
    assert rid == 7
    assert before <= lo <= after, "窗口下界不是「现在」—— 多半是两个边界传反了"
    assert hi - lo == timedelta(minutes=60), f"窗口不是 60 分钟，是 {hi - lo}"
    assert mins == 60, "DATE_SUB 用的分钟数和窗口对不上"
    # 参数是按位置填进去的：SQL 里下界必须排在上界前面，否则上面那两个值会填反
    assert sql.index("end_time > %s") < sql.index("end_time <= %s")


def test_两个推送时间戳各自写各自的列():
    """【这两行是全项目最容易复制粘贴写反的形状】上下紧挨着、只差一个字符串
    字面量，而且 _mark_pushed 是把列名拼进 SQL 的，写反了照样跑得通。
      标反成 notified_at       → final_notified_at 永远是 NULL，同一件拍卖
                                 每一轮都被提醒一次，直到它结束，一小时几十条
      标反成 final_notified_at → 第一条永远标不上，每轮重推同一批，很快撞上
                                 notify_max_per_round 被整批烧掉
    上面那条行为用例只看得到 notify 调了哪个方法名，看不到这里传了哪个列。
    """
    from db import store

    rows = [{"source": "s", "item_id": "i", "rule_id": 1}]
    assert "SET notified_at = %s" in capture(store.mark_notified, rows)[0]
    assert "SET final_notified_at = %s" in capture(store.mark_final_notified, rows)[0]


# ------------------------------------------------------- 拍卖快结束的第二条

def auction(**kw):
    """一件已经推过第一条、现在只剩不到一小时的捡漏拍卖。"""
    return item(source="yahoo_auction", item_id="z9", bid_count=12,
                end_time=config.now() + timedelta(minutes=40), **kw)


ON = {"notify_url": "u", "notify_on": "deal", "notify_max_per_round": 5,
      "notify_final_min": 60}


def test_快结束的捡漏拍卖会再推一条():
    sent, fake, n = run({}, ON, [], final=[auction()])
    assert n == 1 and len(sent) == 1
    assert fake.final_asked == [60]


def test_第二条的头一个词必须和第一条不一样():
    """【不换词就白推了】两条正文其余部分几乎一模一样 —— 商品名、价格、链接
    全都一致。你在通知栏里看到的是"又是它"，顺手就划掉了，而这一条恰恰是
    唯一一条"现在不点就真没了"。
    """
    first = notify.compose(RULE, auction(), 820000)
    second = notify.compose(RULE, auction(), 820000, final=True)
    assert first.splitlines()[0] != second.splitlines()[0]
    assert second.startswith("⏰")


def test_第二条标的是另一个时间戳():
    """【标错列就只剩一种结果】标回 notified_at 的话 final_notified_at 永远是
    NULL，这件商品会每一轮都被提醒一次，直到它结束 —— 一小时几十条。
    """
    a = auction()
    _, fake, _ = run({}, ON, [], final=[a])
    assert fake.marked_final == [a] and fake.marked == []


def test_提醒关掉时一次都不查():
    """0＝关闭。查了不发也不行：那是每轮每条规则一次白跑的查询。"""
    _, fake, n = run({}, {**ON, "notify_final_min": 0}, [item()])
    assert fake.final_asked == [] and n == 1


def test_新命中井喷不能把快结束提醒一起吞掉():
    """【这是分开判上限的全部理由】放宽一次规则就有几十件新命中，
    而同一轮里那条「还剩 20 分钟」恰恰是最不能丢的。
    """
    flood = [item(item_id=f"m{i}") for i in range(9)]
    a = auction()
    sent, fake, n = run({}, ON, flood, final=[a])
    assert n == 1, "新命中撞了上限，快结束提醒也跟着被跳过了"
    assert "⏰" in sent[0][2]
    assert fake.marked == flood and fake.marked_final == [a]


def test_两批之间也要留够间隔():
    """【GAP 按"上一条发出去多久"算】分两个循环发的话，第二批的头一条不等，
    会和第一批的末条挤在同一秒里出去 —— 正好撞上 Slack 的 1 条/秒。
    """
    sent, _, n = run({}, ON, [item()], final=[auction()])
    assert n == 2
    assert SLEPT == [notify.GAP], f"两批之间没等：{SLEPT}"
