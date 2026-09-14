"""推送。

【为什么这条路径值得测】它的两种失败都很难在日志里看出来：
  发不出去 —— JSON 模板被商品标题里的引号撑破，对方回 400，
              日志里只有一个没头没尾的状态码
  发太多了 —— 刚填上 notify_url 那一刻库里几十件老命中一起轰出去，
              人被淹一次就再也不开推送了
下面这几条锁的就是这两件事，外加「推送坏了不能拖垮抓取」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import notify  # noqa: E402

RULE = {"id": 1, "name": "RTX 5090 单卡"}


def item(**kw):
    base = {"source": "mercari", "item_id": "m1", "rule_id": 1, "name": "RTX 5090",
            "price": 700000, "is_deal": 1, "deal_pct": 85, "bid_count": None}
    return {**base, **kw}


class FakeStore:
    """只实现 notify 真正用到的那几个方法。"""

    def __init__(self, settings, rows):
        self.settings, self.rows = settings, rows
        self.marked = []

    def get_settings(self):
        return self.settings

    def pending_notify(self, rule_id, only_deal):
        return [r for r in self.rows if r["is_deal"]] if only_deal else list(self.rows)

    def mark_notified(self, rows):
        self.marked.extend(rows)

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


def run(monkeypatch_target, settings, rows, sender=None):
    """装好假的 store 和假的 post，跑一次 push_new，返回 (发出去的内容, FakeStore)。"""
    sent = []
    fake = FakeStore(settings, rows)
    SLEPT.clear()
    monkeypatch_target["store"], monkeypatch_target["post"] = notify.store, notify.post
    saved_time = notify.time
    notify.store = fake
    notify.post = sender or (lambda url, tpl, text: sent.append((url, tpl, text)))
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
    def boom(url, tpl, text):
        raise RuntimeError("connection refused")

    sent, fake, n = run({}, {"notify_url": "u", "notify_on": "deal",
                             "notify_max_per_round": 5}, [item()], sender=boom)
    assert n == 0                      # 一条都没发成功
    assert len(fake.marked) == 1       # 但已经标掉了，下轮不会再试


def test_单条发送失败不影响后面几条():
    calls = []

    def flaky(url, tpl, text):
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
