"""面板摘要里的两句话，写错了不会报错，只会让人纳闷。

  捡漏线  手动价一填就盖过百分比（core/matcher.is_deal），但成交页原先只按百分比算 ——
          四条规则都填了手动价，显示的全是不生效的那条线。
  预算    price_max=0 是「不限」，写成「¥500,000〜¥0」看着像配错了。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.ui import budget_text, deal_line  # noqa: E402


def test_手动价盖过百分比():
    assert deal_line({"deal_price": 950_000, "deal_ratio": 90}, 800_000) == "手动捡漏价 ¥950,000"


def test_没手动价时按中位数百分比():
    assert deal_line({"deal_price": 0, "deal_ratio": 90}, 800_000) == "低于 ¥720,000 算捡漏"


def test_算不出就空():
    assert deal_line({"deal_price": 0, "deal_ratio": 90}, None) == ""
    assert deal_line({"deal_price": 0, "deal_ratio": 0}, 800_000) == ""


def test_预算上限0是不限():
    assert budget_text({"price_min": 500_000, "price_max": 1_200_000}) == "¥500,000〜¥1,200,000"
    assert budget_text({"price_min": 500_000, "price_max": 0}) == "¥500,000 起，不限上限"
    assert budget_text({"price_min": 0, "price_max": 0}) == "不限"


def test_成交页和命中页用同一个捡漏线函数():
    import inspect
    from web import ui
    def real(r):                                  # ui.refreshable 包着真函数在 .func；普通函数原样
        return getattr(r, "func", r)

    for fn in (real(ui.sold_view), real(ui.hits_view)):
        src = inspect.getsource(fn)
        assert "deal_line(rule, med)" in src, f"{fn.__name__} 没走 deal_line，两页会各说各的"
