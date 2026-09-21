"""七个视图各在【真库】上渲染一遍。连不上库就整个跳过 —— 平时的 0.7 秒测试集不依赖它。

【为什么要有它】上面那些用例全是假 store，盯得住"发几条 SQL"，盯不住
"这条 SQL 真跑起来行吗"「新建一条规则、还没扫过就打开规则页会不会 KeyError」这类事。
这是唯一一条把 web/ui.py 的每个视图连着 db/store.py 的真 SQL 一起走一遍的路。

跑法：.venv/bin/python -m pytest tests/test_render_live.py -q   （需要 .env 里的库能连上）
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402


def _db_ok() -> bool:
    try:
        store.query("SELECT 1")
        return True
    except Exception:           # noqa: BLE001 - 连不上就跳过
        return False


pytestmark = pytest.mark.skipif(not _db_ok(), reason="数据库连不上，跳过真库渲染")

# 每页最多允许发几条 SQL —— 超了就是有人在循环里加了"顺手查一下"
# 【规则 4→5、设置 2→3 是 2026-09-17 全局拉黑带来的】规则页页尾那块「拉黑列表」
# 自己读一次 blocked_seller（和命中页的 hidden_items 同性质，整行取、没法折进缓存）；
# 设置页的 all_settings 里有一次 get_settings(force=True)，现在会连带读一次那张表。
# 别的五页是 0 增量：get_rules → _with_settings → get_settings 已经把列表读进缓存，
# blocked_sellers() 只是读缓存。数字要动必须先想清楚是不是又有人在循环里加了查询。
BUDGET = {"命中": 8, "追踪": 4, "标记": 2, "成交": 5, "全部": 2, "规则": 5, "设置": 3}


@pytest.mark.parametrize("name", list(BUDGET))
def test_视图在真库上渲染_且SQL条数在预算内(name):
    from nicegui import Client, ui
    from nicegui.page import page as _page
    from web import ui as wui

    calls = []
    saved_q, saved_e = store.query, store.execute
    store.query = lambda sql, args=(): (calls.append(sql), saved_q(sql, args))[1]
    store.execute = lambda sql, args=(): (calls.append(sql), saved_e(sql, args))[1]
    # 预热：这一句现在也把拉黑列表读进了同一份缓存。删掉它会让好几页的计数
    # 一起变（那是冷缓存的真实代价），不是 bug。
    store.get_settings(force=True)
    calls.clear()
    fns = {
        "命中": lambda: wui.hits_view(ui.element()),
        "追踪": wui.track_view,
        "标记": wui.marks_view,
        "成交": wui.sold_view,
        "全部": lambda: wui.all_view(None, "全部"),
        "规则": lambda: wui.rules_view(ui.element()),
        "设置": wui.settings_view,
    }
    try:
        with Client(_page(f"/_t_{name}")):
            fns[name]()
    finally:
        store.query, store.execute = saved_q, saved_e
    assert len(calls) <= BUDGET[name], f"{name}页发了 {len(calls)} 条 SQL（预算 {BUDGET[name]}）：\n" + "\n".join(
        " ".join(c.split())[:100] for c in calls)
