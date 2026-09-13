"""匹配逻辑回归测试。用例全部是实测从 Mercari 搜 "RTX 5090" 真实返回的商品。

这套测试守的是这个项目唯一的核心指标：精准度。改了 normalize 或 matcher 必须先跑它。
    ./run.sh test
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.matcher import flag_desc, is_deal, judge_snap  # noqa: E402
from core.normalize import norm, words  # noqa: E402

# 一条典型的 5090 单卡规则，和 tools/seed.py 里写进库的种子值保持一致
RULE = {
    "include_all": "5090",
    "include_any": "",
    "exclude_any": "ゲーミングPC,ノート,laptop,一体型,ジャンク,部品取り,故障,不動,"
                   "箱のみ,空箱,クーラー,ヒートシンク,ブラケット,ライザー,電源ユニット,"
                   "水枕,水冷ヘッド",
    "warn_desc": "ジャンク,部品取り,動作未確認,不動,マイニング,採掘",
    "price_min": 250000,
    "price_max": 450000,
    "condition_ids": "",
    "allow_shops": 0,
    "check_desc": 1,
}


def snap(name, price, item_type="user", condition_id=3):
    return {"name": name, "price": price, "item_type": item_type, "condition_id": condition_id}


# ---------------------------------------------------------------- 归一化

def test_全角半角空格连字符都归一到同一个串():
    assert norm("RTX 5090") == norm("RTX5090") == norm("ＲＴＸ５０９０") == norm("rtx-5090")


def test_半角片假名转全角():
    assert norm("ｼﾞｬﾝｸ") == norm("ジャンク")


def test_长音符不被当成符号删掉():
    # 「ー」是 Unicode 类别 Lm，isalnum() 为 True。删掉的话 ゲーミング 就匹配不上了
    assert norm("ゲーミングPC") == "ゲーミングpc"


def test_词表分隔符认半角全角逗号顿号换行():
    assert words("ジャンク,部品取り、故障；不動\n空箱") == \
           ["ジャンク", "部品取り", "故障", "不動", "空箱"]


# ---------------------------------------------------------------- 第 1 层：不入库

def test_标题里根本没有型号的直接丢弃():
    # 实测：搜 RTX 5090 返回了这条，Mercari 的搜索是模糊的
    v = judge_snap(RULE, snap("NVIDIA V100 32GB ファン付き qwen27b/動画生成", 153000))
    assert v["keep"] is False and v["reject_reason"] == "no_keyword"


def test_型号写成全角也算命中不被丢弃():
    v = judge_snap(RULE, snap("ＲＴＸ５０９０ 32GB 美品", 380000))
    assert v["keep"] is True and v["matched"] == 1


# ---------------------------------------------------------------- 第 2 层：入库标 matched=0

def test_整机被标题排除词拦下但仍然入库():
    v = judge_snap(RULE, snap("ゲーミングPC Ultra 9 285K RTX 5090 メモリ64GB", 1498000))
    assert v["keep"] is True and v["matched"] == 0
    assert v["reject_reason"] == "excluded_title"


def test_散热器被排除词拦下():
    v = judge_snap(RULE, snap("未使用5個 INNO3D RTX 5090 X3 OC 32GB GPUクーラー", 18000))
    assert v["matched"] == 0 and v["reject_reason"] == "excluded_title"


def test_ジャンク品被排除():
    v = judge_snap(RULE, snap("Palit GeForce RTX 5090 GPUコア，メモリ無しジャンク品", 12800))
    assert v["matched"] == 0 and v["reject_reason"] == "excluded_title"


def test_笔记本靠价格上限兜住():
    # 标题里没有「ノート」，排除词抓不到；620000 超出单卡区间，由 price_over 挡下
    v = judge_snap(RULE, snap("ROG Strix SCAR 16 G635LX RTX5090 64GB 3T", 620000))
    assert v["matched"] == 0 and v["reject_reason"] == "price_over"


def test_Shops商家品默认不收():
    v = judge_snap(RULE, snap("【新品】グラフィックボード GeForce RTX 5090", 939800, item_type="shop"))
    assert v["matched"] == 0 and v["reject_reason"] == "shop_item"


def test_低于价格下限的配件():
    v = judge_snap(RULE, snap("RTX5090 用 サポートステー", 3000))
    assert v["matched"] == 0 and v["reject_reason"] == "price_under"


def test_排除原因优先级_永久性原因排在价格前面():
    # 同时触发 excluded_title 和 price_over，应记更有信息量的那个：
    # 它永远不会翻身，而 price_over 的降价后会重新命中
    v = judge_snap(RULE, snap("ゲーミングPC RTX 5090 搭載", 1498000))
    assert v["reject_reason"] == "excluded_title"


def test_品相白名单():
    r = dict(RULE, condition_ids="1,2")
    assert judge_snap(r, snap("RTX 5090 32GB", 380000, condition_id=4))["reject_reason"] == "condition"
    assert judge_snap(r, snap("RTX 5090 32GB", 380000, condition_id=2))["matched"] == 1


def test_排除词不能误杀ASUS_DUAL():
    # 词表里曾经有「5090D」（中国特供 D 版）。归一化会把「RTX 5090 DUAL」变成
    # rtx5090dual，里面正好含 5090d —— ASUS DUAL 这个主流型号会被当成 D 版杀掉。
    # D 版在日本实测 0 件，为防一个不存在的东西误杀主流型号不划算，所以词表里不放。
    assert judge_snap(RULE, snap("RTX 5090 DUAL 32GB", 380000))["matched"] == 1
    assert judge_snap(RULE, snap("ASUS DUAL GeForce RTX 5090 OC", 400000))["matched"] == 1


# ---------------------------------------------------------------- 合格品

def test_一块正常的单卡应该命中():
    v = judge_snap(RULE, snap("MSI GeForce RTX 5090 GAMING TRIO OC 32GB", 380000))
    assert v == {"keep": True, "matched": 1, "reject_reason": ""}


# ------------------------------------------------- 描述警示标签（只提示，不否决）

def test_描述命中只打标签_绝不影响是否合适():
    # 整个设计的核心取舍：误杀是沉默的（商品不会出现在任何列表里），
    # 漏筛是可见的（带黄标进来的坏货你扫一眼就跳过）。所以宁可漏筛。
    row = dict(snap("MSI GeForce RTX 5090 GAMING TRIO OC 32GB", 380000),
               desc_checked=1, description="ファンから異音あり、ジャンク扱いでお願いします。")
    assert judge_snap(RULE, row)["matched"] == 1          # 仍然算合适
    assert flag_desc(RULE, row["description"]) == "ジャンク"   # 只是挂个标签


def test_描述里的ジャンク会被标出来():
    assert flag_desc(RULE, "動作品ですが、ファンから異音。ジャンク扱いでお願いします。") == "ジャンク"


def test_描述干净则没有标签():
    assert flag_desc(RULE, "使用期間3ヶ月、動作確認済み。保証書あります。") == ""


def test_描述为空时没有标签():
    assert flag_desc(RULE, "") == ""


def test_不判断否定语境_卖家否认时照样挂标签():
    # 【这是有意为之，不是 bug】曾经为此做过一套日语否定词表 + 逐句切分，后来砍了：
    # 标签本来就只是「你来看一眼」，误挂一个的代价是多扫一眼，
    # 而那套逻辑要额外维护一份否定词表、还要处理句子边界，不值当。
    # 嫌标签吵就把那个词从 warn_desc 里删掉 —— 词表是面板上可编辑的。
    assert flag_desc(RULE, "マイニング等の負荷が高い用途では使用しておらず。") == "マイニング"
    assert flag_desc(RULE, "ジャンク品ではありません。動作確認済みです。") == "ジャンク"


def test_标签回显你填的原词而不是归一化结果():
    # 面板上看到「ジャンク」比看到归一化后的串更容易发现自己词表哪里写错了
    assert flag_desc(dict(RULE, warn_desc="ジャンク"), "これはジャンクです。") == "ジャンク"


def test_多个警示词命中时一起列出():
    assert flag_desc(RULE, "ジャンク品です。部品取りにどうぞ。") == "ジャンク,部品取り"


def test_警示词表归一化_全角半角写法都认():
    assert flag_desc(dict(RULE, warn_desc="ジャンク"), "これは ｼﾞｬﾝｸ 品です。") == "ジャンク"


def test_描述警示词和标题排除词是分开的():
    # 「ゲーミングPC」只在 exclude_any 里，不在 warn_desc 里 ——
    # 描述里的一句「ゲーミングPCにも使えます」不该给一块单卡挂标签
    assert flag_desc(RULE, "ゲーミングPCにも使えます。動作確認済み。") == ""


# ---------------------------------------------------------------- 捡漏判定

def test_低于中位数85pct算捡漏():
    assert is_deal(300000, 400000, 85) == (1, 75)


def test_接近中位数不算捡漏但仍给出百分比():
    assert is_deal(380000, 400000, 85) == (0, 95)


def test_样本不足时不判也不出百分比():
    assert is_deal(300000, None, 85) == (0, None)


def test_关掉捡漏判定仍然给百分比做参考():
    assert is_deal(300000, 400000, 0) == (0, 75)


# ---------------------------------------------------------------- 手动捡漏价

def test_手动捡漏价盖过百分比():
    """填了手动价，deal_ratio 就不生效 —— 面板摘要也必须跟着改口径，
    否则你会对着「低于 ¥706,500 算捡漏」纳闷为什么 ¥70 万的没标。"""
    # 中位 78.5万、百分比 90%（线在 70.65万）；手动价压到 70万
    assert is_deal(700_000, 785_000, 90, 700_000) == (0, 89)   # 不低于手动价 → 不是捡漏
    assert is_deal(650_000, 785_000, 90, 700_000) == (1, 82)   # 低于手动价 → 是
    # 同样的价，不填手动价时按百分比判就是捡漏 —— 证明确实是手动价在起作用
    assert is_deal(700_000, 785_000, 90, 0) == (1, 89)


def test_手动价不依赖成交样本():
    """【这才是手动价存在的理由】百分比那套要先有中位数，中位数要先攒够成交样本。
    规则刚建、或某个型号本来就成交稀少时，百分比整个不工作、捡漏徽标永远不亮，
    而你心里其实是有价的。"""
    assert is_deal(650_000, None, 90, 700_000) == (1, None)
    assert is_deal(720_000, None, 90, 700_000) == (0, None)
    # 对照：没有手动价时，没中位数就完全判不了
    assert is_deal(650_000, None, 90, 0) == (0, None)


def test_手动价填0等于没填():
    """0 是「不用手动价」，不是「低于 0 才算捡漏」（那会让捡漏永远不亮）。"""
    assert is_deal(300_000, 400_000, 85, 0) == is_deal(300_000, 400_000, 85)


def test_百分比照常给出_pct_供参考():
    """pct 是给人看的参考，和判定是两件事：手动价生效时它照样按中位数算。"""
    assert is_deal(650_000, 785_000, 0, 700_000)[1] == 82


# ---------------------------------------------------------------- 追踪的终态

def test_进终态就摘掉追踪_每条路径都要覆盖():
    """【为什么写在 store 层而不是调用方】把 status 改成 sold_out/gone 的路径有两条：
    set_status（poller 里 6 个调用点都走它）和 update_tracked（追踪刷新自己写）。
    放到调用方去摘迟早漏掉一条，而漏掉的表现是「有商品永远摘不掉」——
    追的件数是"每天多烧多少请求"的分母，挂着死货会让那个数字失真。
    """
    import inspect

    from db import store

    assert store.TERMINAL == ("sold_out", "gone")
    for fn in (store.set_status, store.update_tracked):
        src = inspect.getsource(fn)
        assert "tracked_at = NULL" in src, f"{fn.__name__} 进终态时没摘追踪"
        assert "TERMINAL" in src, f"{fn.__name__} 应该用 TERMINAL 判终态，别各写各的"
