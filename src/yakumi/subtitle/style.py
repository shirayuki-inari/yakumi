"""字幕标点惯例（只在导出时套用，不改动保存的原文和译文）。

中文字幕的惯例：
- 不用逗号和句号：句中的 ，。；（以及夹在中文里的半角 , . ;）换成一个空格，行尾的直接去掉；
- 行尾的顿号、冒号也去掉；句中的顿号用于列举，保留；
- 问号、感叹号保留，统一为全角；省略号统一为 ……；
- 引号、书名号等成对的标点保留。
日文字幕同理：句中的 、。 换成全角空格，行尾的去掉；省略号保持原样。
其他语言不处理。

保存的数据保留完整标点，因为整句翻译和译文切分依赖标点；校对时看到的也是完整的句子。
"""

from __future__ import annotations

import re

STYLES = ("subtitle", "keep")

_KANA_RE = re.compile(r"[぀-ヿ]")
_HAN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_CJK = r"[぀-ヿ㐀-䶿一-鿿豈-﫿]"
_OPENERS = "「『“‘《（【〈"
_CLOSERS = "」』”’》）】〉"


_ZH_PUNCT_RE = re.compile(r"[，。！？；：、…]")


def detect(text: str) -> str | None:
    if _KANA_RE.search(text):
        return "ja"
    # 只有拉丁字母、但用了中文标点的行（例如 “Konkoyo。”）也按中文处理
    if _HAN_RE.search(text) or _ZH_PUNCT_RE.search(text):
        return "zh"
    return None


def _tidy(text: str, space: str) -> str:
    s = re.escape(space)
    text = re.sub(rf"[ 　]*{s}[ 　]*", space, text)  # 合并连续空格
    text = re.sub(rf"{s}+", space, text)
    text = re.sub(rf"{s}(?=[？！…{re.escape(_CLOSERS)}])", "", text)
    text = re.sub(rf"(?<=[{re.escape(_OPENERS)}]){s}", "", text)
    return text.strip(" 　")


def style_zh(text: str) -> str:
    text = re.sub(r"\.{3,}|。{2,}|⋯+|…+", "……", text)
    text = re.sub(rf"(?<={_CJK})\?|\?(?={_CJK})", "？", text)
    text = re.sub(rf"(?<={_CJK})!|!(?={_CJK})", "！", text)
    text = re.sub(rf"(?<={_CJK})[,.;]|[,;](?={_CJK})", "，", text)
    text = text.rstrip()
    text = re.sub(r"[，。；、：,;:]+$", "", text)
    text = re.sub(r"[，。；]+", " ", text)
    return _tidy(text, " ")


def style_ja(text: str) -> str:
    text = text.rstrip()
    text = re.sub(r"[、。，．]+$", "", text)
    text = re.sub(r"[、。，．]+", "　", text)
    return _tidy(text, "　")


def style_line(text: str, style: str = "subtitle") -> str:
    if style == "keep" or not text:
        return text
    if style != "subtitle":
        raise ValueError(f"未知的标点风格：{style}（可选 subtitle / keep）")
    lang = detect(text)
    if lang == "zh":
        return style_zh(text) or text
    if lang == "ja":
        return style_ja(text) or text
    return text
