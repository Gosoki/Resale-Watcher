"""规则重放：拿库里已经抓到的商品，重新跑一遍当前规则，看判定会变成什么样。

【为什么需要它】排除词是这个项目里最容易改错的东西，而改错的代价不对称：
加宽了只是多几条 matched=0 的噪音，你在面板上一眼能看见；
加狠了会把好货误杀成 excluded_title，而误杀是沉默的，你永远不会注意到。
这个工具就是把沉默的那一半翻出来 —— 尤其是最后那段「潜在误杀」。

不发任何网络请求，所以想怎么试都行。加 --apply 才写回数据库。

    .venv/bin/python tools/replay.py          # 全部规则，只预演
    .venv/bin/python tools/replay.py 1        # 只看规则 1
    .venv/bin/python tools/replay.py 1 --apply  # 把新判定写回库
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.matcher import judge_snap  # noqa: E402
from core.normalize import norm, words  # noqa: E402
from db import store  # noqa: E402


def replay(rule: dict, apply: bool) -> None:
    rid = rule["id"]
    rows = store.query(
        "SELECT source, item_id, name, price, item_type, condition_id, matched, "
        "reject_reason, desc_checked FROM item WHERE rule_id = %s", (rid,))
    if not rows:
        print(f"规则 #{rid}「{rule['name']}」库里还没有商品，先跑一轮再来。\n")
        return

    print(f"=== #{rid} {rule['name']} ===")
    print(f"    预算 ¥{rule['price_min']:,}〜¥{rule['price_max']:,}，库里 {len(rows)} 件\n")

    dist, flips = Counter(), []
    for r in rows:
        v = judge_snap(rule, r)
        dist[v["reject_reason"] or "(合适)"] += 1
        if v["matched"] != r["matched"]:
            flips.append((r, v))
        r["_new"] = v

    print("  判定分布：")
    for k, n in dist.most_common():
        print(f"    {k:<16} {n:>4} 件")

    if flips:
        print(f"\n  和库里现状相比有 {len(flips)} 件会改判：")
        for r, v in flips[:20]:
            arrow = "命中 → 排除" if r["matched"] else "排除 → 命中"
            print(f"    {arrow}  ¥{r['price']:>9,} [{v['reject_reason'] or '合适'}] {r['name'][:44]}")

    hits = sorted((r for r in rows if r["_new"]["matched"]), key=lambda r: r["price"])
    print(f"\n  命中 {len(hits)} 件：")
    for r in hits[:25]:
        print(f"    ¥{r['price']:>9,} [{r['source']}] {r['name'][:48]}")

    # 这一段是本工具的重点：被排除、但价格正好落在你预算里的商品。
    # 真正的误杀只会出现在这里 —— 价格不合适的本来你也不会买。
    ex = words(rule["exclude_any"])
    suspects = [r for r in rows
                if r["_new"]["reject_reason"] == "excluded_title"
                and rule["price_min"] <= r["price"] <= (rule["price_max"] or 10 ** 9)]
    print(f"\n  ⚠ 潜在误杀：预算内却被标题排除词拦下的 {len(suspects)} 件"
          f"{'（逐条核对下面命中的词对不对）' if suspects else ' —— 没有，排除词是干净的'}")
    for r in sorted(suspects, key=lambda r: -r["price"])[:15]:
        hit = [w for w in ex if w in norm(r["name"])]
        print(f"    ¥{r['price']:>9,} [{','.join(hit[:3])}] {r['name'][:50]}")

    if apply:
        # 【直接调 poller.revalidate，不要自己再写一遍 UPDATE】
        # 两份实现已经分叉过一次：这里只写 matched/reject_reason，而 revalidate
        # 还会重算 desc_warn —— 于是 --apply 之后下一轮轮询又把结果改一次，
        # 你看到的和最终入库的不是同一个东西。
        from core.poller import revalidate
        n = revalidate(rule)
        print(f"\n  ✔ 已把新判定写回数据库（改判 {n} 件 / 共 {len(rows)} 件）")
    print()


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--apply"]
    apply = "--apply" in sys.argv
    rules = [store.get_rule(int(args[0]))] if args else store.get_rules()
    for rule in filter(None, rules):
        replay(rule, apply)


if __name__ == "__main__":
    main()
