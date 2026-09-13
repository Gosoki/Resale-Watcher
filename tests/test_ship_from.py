"""发货地（都道府県）。

【为什么要归一】三个源给的形态不一样：メルカリ 给 `{"name": "愛知県"}`、
ヤフオク 给「香川県」、而 Yahoo!フリマ 给的是大写罗马字 "KAGAWA"。
不在数据源层就地归一的话，库里会一半日文一半罗马字，
面板上的「東京都」判断得写两套，而且哪天漏写一套就是静默失效。

【为什么没有标签不等于不在东京】发货地只在【详情】响应里有，搜索结果里三个源都没有。
所以没拉过详情的商品这一列恒为空，那是「未知」不是「不在东京」——
和品相白名单、卖家黑名单同一条原则：不知道 ≠ 不是。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.base import _ROMAJI, pref_of  # noqa: E402


def test_罗马字归一成日文():
    assert pref_of("TOKYO") == "東京都"
    assert pref_of("KAGAWA") == "香川県"
    assert pref_of("HOKKAIDO") == "北海道"      # 不是「県」
    assert pref_of("OSAKA") == "大阪府"          # 府
    assert pref_of("KYOTO") == "京都府"


def test_大小写和空白都容忍():
    assert pref_of("tokyo") == "東京都"
    assert pref_of("  TOKYO  ") == "東京都"


def test_认不出的原样留着_不抹成空():
    """抹成空就等于谎报「未知」，而未知会让面板不打标签 ——
    真出现没见过的取值时，留着原文才看得出是对方加了新值。"""
    assert pref_of("NEW_AREA_X") == "NEW_AREA_X"


def test_空值就是未知():
    for v in (None, "", "   "):
        assert pref_of(v) == ""


def test_四十七个都道府県一个不少():
    """少一个就是那个县的商品永远显示成罗马字。"""
    assert len(_ROMAJI) == 47
    assert len(set(_ROMAJI.values())) == 47      # 没有重复映射


def test_面板用的东京常量和归一结果对得上():
    """两边对不上的话，东京的商品永远不会被打标签，而且没有任何报错。"""
    from web.ui import TOKYO
    assert pref_of("TOKYO") == TOKYO


def test_带市区町村的要归一到都道府県():
    """【这条是踩出来的】ヤフオク 有卖家填「東京都 板橋区」，
    而面板用 == "東京都" 比对 —— 这件在东京的商品当场被漏掉，
    而且没有任何报错，你只会觉得「标签时灵时不灵」。"""
    assert pref_of("東京都 板橋区") == "東京都"
    assert pref_of("大阪府 堺市") == "大阪府"


def test_不能用切到第一个都道府県字的写法():
    """「京都府」的「都」在第二个字 —— 按字符切会切成「京都」，
    于是京都府的商品永远匹配不上任何一个已知都道府県。
    所以只能拿 47 个已知名做前缀匹配。"""
    assert pref_of("京都府") == "京都府"
    assert pref_of("京都府 左京区") == "京都府"
    # 反过来：東京都 不能被误判成 京都府
    assert pref_of("東京都") == "東京都"


# ---------------------------------------------------------------- 售出状态

def test_フリマ的详情页和搜索接口用两套字段名():
    """【这条是踩出来的，代价很大】同一个源：
      搜索接口   itemStatus='OPEN'   （没有 status 键）
      商品详情页 status='SOLD'        （没有 itemStatus 键）
    detail() 原先照搬了搜索那套名字，于是【每一件】フリマ 商品的详情都返回空状态。
    售出对账拿到空串走「保持原状下轮再看」—— 这个源的商品永远确认不了卖掉：
    卖掉的货一直挂在命中页上，tracked 成交样本（最准的市价依据）一条都采不到。
    库里实测：yahoo_flea 124 件在售、0 件已售出。
    """
    import inspect

    from sources import yahoo_flea

    src = inspect.getsource(yahoo_flea.YahooFlea.detail)
    assert 'item.get("status")' in src, "detail() 必须读详情页的 status 字段"


def test_两个字段名的映射都认():
    """留着 itemStatus 兜底：万一哪天详情页改回去，不至于又整源失效。"""
    m = {"OPEN": "on_sale", "SOLD": "sold_out"}
    for item, want in (({"status": "SOLD"}, "sold_out"),
                       ({"status": "OPEN"}, "on_sale"),
                       ({"itemStatus": "SOLD"}, "sold_out"),
                       ({"status": None, "itemStatus": "OPEN"}, "on_sale"),
                       ({}, "")):
        got = m.get(item.get("status") or item.get("itemStatus"), "")
        assert got == want, (item, got, want)
