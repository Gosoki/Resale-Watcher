"""文本归一化 —— 「精准」的地基。

Mercari 的标题是人手打的，同一块卡实测有这些写法：
    RTX 5090 / RTX5090 / ＲＴＸ５０９０ / rtx-5090 / GeForce　RTX　5090（全角空格）
「ジャンク」也有 ジャンク / ｼﾞｬﾝｸ / じゃんく 三种。
如果不归一化，你就得在规则表里手工穷举所有变体，那张表会变得没法维护。

做法：NFKC → 小写 → 只保留字母数字假名汉字（空格和 -・/ 这类符号全删）。
归一化后一律用子串判断，所以规则里填「5090」就能命中上面全部写法。
"""
import re
import unicodedata

_SPLIT = re.compile(r"[,，、;；\n\r\t]+")


def norm(text: str | None) -> str:
    if not text:
        return ""
    # NFKC 一步搞定全角英数→半角、半角片假名→全角片假名（ｼﾞｬﾝｸ→ジャンク）。
    s = unicodedata.normalize("NFKC", text).lower()
    # isalnum() 对假名和汉字都是 True，对长音符「ー」也是 True（Unicode 类别 Lm），
    # 所以 ゲーミング 不会被拆坏；空格、-、・、/、【】等符号则全部消失。
    return "".join(ch for ch in s if ch.isalnum())


def word_pairs(csv: str | None) -> list[tuple[str, str]]:
    """返回 [(你填的原词, 归一化后的词), …]。

    匹配要用归一化后的，但面板上的警示标签要显示你填进去的那个写法 ——
    回显「ジャンク」比回显归一化结果更容易看懂自己的词表哪里写错了。
    """
    if not csv:
        return []
    return [(p.strip(), norm(p)) for p in _SPLIT.split(csv) if norm(p)]


def words(csv: str | None) -> list[str]:
    """把规则表里逗号分隔的词表拆开并逐个归一化。半角/全角逗号、顿号、分号、换行都认。"""
    return [n for _raw, n in word_pairs(csv)]



def id_tokens(csv: str | None) -> list[str]:
    """把逗号分隔的【标识符】拆开，只去空白，【保留原样大小写】。

    下面的 ids() 是它的小写版，那是【比对】用的口径。要入库、要显示给人看的
    时候必须用这一个：ヤフオク 和 メルカリShops 的 ID 是大小写敏感的 base62，
    压成小写之后那串字符就再也复制不回源站打开了。
    """
    if not csv:
        return []
    return [t.strip() for t in _SPLIT.split(csv) if t.strip()]


def ids(csv: str | None) -> set[str]:
    """把逗号分隔的【标识符】拆开，只做去空白 + 转小写。

    【刻意不走 norm()】norm() 会把非字母数字的字符全删掉，那是为商品标题设计的：
    「RTX 5090」和「rtx-5090」应该视为同一个词。但卖家 ID 是标识符不是词 ——
    实测 ヤフオク 的 ID 形如 `2yhr98NDVi1eGuLhNbYtU5Z6`，Mercari 是纯数字，
    Yahoo!フリマ 是 `p58365705`。这些本来就没有符号可剥，剥了反而可能把两个
    不同的 ID 抹成同一个。转小写是为了容忍手抄时的大小写出入：
    实测库里 179 个卖家 ID 小写化之后【没有任何碰撞】。
    """
    return {t.lower() for t in id_tokens(csv)}
