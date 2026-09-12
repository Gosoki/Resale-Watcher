"""写入两条起步用的监控规则。幂等：按规则名判重，重复跑不会插重复。

sources 留空＝搜全部数据源（メルカリ + Yahoo!フリマ）。

这些只是起点 —— 价格区间、排除词都到面板上按你的实际需求改。
排除词的取舍原则：宁可宽一点让它入库标 matched=0（你还能在面板「全部」页里回看），
也别写得太狠把好货误杀了 —— 误杀是不留痕迹的。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402

# 单卡通用排除词。每一条都对应实测在 Mercari 上真实见过的噪音，不是凭空想的：
#
#   CPU 型号   ryzen/ultra9/14900/9950x/275hx … 整机和笔记本必写 CPU，单卡标题绝不会出现，
#              是区分「整机」和「单卡」最可靠的信号（比「ゲーミングPC」这种词好用得多）
#   整机品牌   galleria/alienware/frontier/raytrek/ドスパラ/tsukumo
#   笔记本     ノート/laptop/インチ/scar   ——「18インチ」这种尺寸单卡不会标
#   空壳废品   基板なし/pcbなし/チップ無し/補修用/交換用/インテリア
#              实测有一整批「ZOTAC RTX 5090 SOLID OC 基板なし」在卖几千日元，
#              还有「ASUS ROG Astral RTX 5090 ホワイト インテリア」——显卡外壳当摆件卖
#   周边配件   クーラー/ヒートシンク/ブラケット/ライザー/変換ケーブル/箱のみ …
#
# 【刻意没放这几个】
#   「デスクトップPC」——「デスクトップPC用グラボ」是单卡的常见写法，放进去会误杀
#   「i9」——归一化后「MSI 9」也含「i9」，太短会误伤；改用 14900/13900 这种够长的型号
#   「搭載」——「水冷ブロック搭載」可能出现在单卡标题；改用 搭載PC/搭載モデル
#   「5090D」「4090D」（中国特供 D 版）——归一化会把「RTX 5090 DUAL」变成 rtx5090dual，
#     里面正好含 5090d，于是 ASUS DUAL 这个主流型号会被当成 D 版杀掉。
#     而 D 版在日本 Mercari 上实测 0 件。为防一个不存在的东西去误杀主流型号，不划算。
COMMON_EXCLUDE = (
    # 整机/笔记本的 CPU 型号
    "ryzen,ultra9,ultra7,corei9,corei7,14900,13900,12900,"
    "9950x,9900x,9800x,7950x,7800x,265k,275hx,285k,"
    "13700,14700,12700,13600,14600,13500,13400,14400,12400,"
    # 【存储容量是最好用的整机信号】显卡标的是显存（24GB/32GB），永远不会出现 TB 和 SSD。
    # 笔记本品牌列举不完（实测「Razer Blade 18 / RTX 4090/32GB/2TB」就漏过了），
    # 但它们全都会标硬盘容量 —— 这一条比堆品牌名管用得多。
    "1TB,2TB,4TB,8TB,512GB,SSD,HDD,"
    # 整机/笔记本品牌与形态
    "galleria,alienware,frontier,raytrek,ドスパラ,tsukumo,"
    "razer,blade,thinkpad,daiv,legion,predator,omen,victus,raider,vector,"
    "ゲーミングPC,自作PC,BTO,ノート,laptop,一体型,インチ,SCAR,"
    "搭載PC,搭載モデル,PC一式,ハイエンドPC,"
    # 空壳 / 废品 / 补修用
    "ジャンク,部品取り,故障,不動,基板なし,PCBなし,チップ無し,チップなし,"
    "GPUコア,補修用,交換用,インテリア,"
    # 周边 / 配件 / 只有箱子
    "箱のみ,空箱,箱1点,付属品のみ,ステッカー,ぬいぐるみ,"
    "クーラー,ヒートシンク,水枕,水冷ヘッド,ブラケット,ライザー,"
    "サポートステー,変換ケーブル,延長ケーブル,電源ユニット,ファンのみ"
)

# 描述排除词只放强信号：这些词出现在描述里，基本可以确定这件东西有问题。
# 不放「ゲーミングPC」这类 —— 描述里一句「ゲーミングPCにも使えます」不代表它是整机。
# 描述警示词：命中【只打黄标，不毙商品】，你在面板上点开自己判断。
# 所以这里可以放得宽一点 —— 宁可多挂个标签让你看一眼，也别静悄悄毙掉一块好卡。
# 「マイニング」「訳あり」这类高误报词正因为不否决了才敢留着：实测它们出现时
# 卖家多半是在否认（「マイニング使用しておらず」「訳あり価格」），做否决词会误杀。
COMMON_WARN_DESC = "ジャンク,部品取り,動作未確認,不動,マイニング,採掘,訳あり,修理,交換済,異音"

RULES = [
    {
        "name": "RTX 5090 单卡", "enabled": 1, "keyword": "RTX 5090", "sources": "",
        "include_all": "5090", "include_any": "",
        "exclude_any": COMMON_EXCLUDE,
        "warn_desc": COMMON_WARN_DESC,
        "price_min": 400000, "price_max": 800000,
        "condition_ids": "", "allow_shops": 0, "check_desc": 1,
        "deal_ratio": 85, "quick_min": 7,
        "note": "区间按实测成交数据定：近30天15件成交 ¥55万〜¥86万，中位 ¥72万。"
                "在售挂单最低要价 ¥101万，是成交中位数的 1.4 倍——基准必须看成交而不是挂单。"
                "上限留到 ¥80万 是让合理价位的都进列表，真正的便宜货由 deal_ratio 标出来",
    },
    {
        "name": "RTX 4090 单卡", "enabled": 1, "keyword": "RTX 4090", "sources": "",
        "include_all": "4090", "include_any": "",
        "exclude_any": COMMON_EXCLUDE,
        "warn_desc": COMMON_WARN_DESC,
        "price_min": 250000, "price_max": 500000,
        "condition_ids": "", "allow_shops": 0, "check_desc": 1,
        "deal_ratio": 85, "quick_min": 7,
        "note": "近30天7件成交 ¥36万〜¥44万，中位 ¥40.6万。样本偏少，中位数还不太稳，"
                "面板上会显示样本数——少于 10 件时这个参考价只能当个大概看",
    },
]


def main() -> None:
    existing = {r["name"] for r in store.get_rules()}
    for r in RULES:
        if r["name"] in existing:
            print(f"跳过（已存在）：{r['name']}")
            continue
        rid = store.insert_rule(r)
        print(f"已写入 #{rid} {r['name']}  ¥{r['price_min']:,}〜¥{r['price_max']:,}")


if __name__ == "__main__":
    main()
