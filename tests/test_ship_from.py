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

from sources.yahoo_flea import _pref  # noqa: E402


def test_罗马字归一成日文():
    assert _pref("TOKYO") == "東京都"
    assert _pref("KAGAWA") == "香川県"
    assert _pref("HOKKAIDO") == "北海道"      # 不是「県」
    assert _pref("OSAKA") == "大阪府"          # 府
    assert _pref("KYOTO") == "京都府"


def test_大小写和空白都容忍():
    assert _pref("tokyo") == "東京都"
    assert _pref("  TOKYO  ") == "東京都"


def test_认不出的原样留着_不抹成空():
    """抹成空就等于谎报「未知」，而未知会让面板不打标签 ——
    真出现没见过的取值时，留着原文才看得出是对方加了新值。"""
    assert _pref("NEW_AREA_X") == "NEW_AREA_X"


def test_空值就是未知():
    for v in (None, "", "   "):
        assert _pref(v) == ""


def test_四十七个都道府県一个不少():
    """少一个就是那个县的商品永远显示成罗马字。"""
    from sources.yahoo_flea import _PREF
    assert len(_PREF) == 47
    assert len(set(_PREF.values())) == 47      # 没有重复映射


def test_面板用的东京常量和归一结果对得上():
    """两边对不上的话，东京的商品永远不会被打标签，而且没有任何报错。"""
    from web.ui import TOKYO
    assert _pref("TOKYO") == TOKYO
