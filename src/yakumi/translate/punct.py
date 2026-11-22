"""译文的标点：提示词里的规则，以及翻译后对中文译文做的确定性整理。

识别出的日文原文常把疑问句写成 “。” 结尾、句中用 “、” 停顿，模型容易照搬到中文译文里。
提示词要求按译文语言的规范使用标点；翻译后再修正几种没有歧义的情况：
- “吗。” → “吗？”（句末的 “吗” 只用于疑问）；
- 单个或三个以上的 … / ⋯ / ...、单个 — 统一为 …… 和 ——；
- 夹在中文里的半角 ? ! , 换成全角；
- “～” 或 “~” 后面多余的句号去掉，“~” 统一为 “～”；
- 字幕条末尾的 “、” 换成 “，”（顿号只用于并列的词语，不会落在一条字幕的末尾）。
句中的 “、” 是否误用无法可靠判断，交给提示词。
"""

from __future__ import annotations

import re

_CJK = r"[぀-ヿ㐀-䶿一-鿿豈-﫿]"
_CLOSERS = "」』”’》）】〉"

PROMPT_RULES = {
    "zh": "标点按中文书写规范，不要照搬原文的标点（识别出的日文常把疑问句写成“。”结尾、句中用“、”停顿）："
          "句中停顿用“，”，顿号“、”只用于并列的词语；疑问句用“？”；省略号用“……”，破折号用“——”。",
}
GENERIC_RULE = "标点按译文语言的书写规范使用，不要照搬原文的标点。"


def is_zh(lang: str | None) -> bool:
    return bool(lang) and lang.lower().split("-")[0] == "zh"


def prompt_rule(target: str) -> str:
    return PROMPT_RULES["zh"] if is_zh(target) else GENERIC_RULE


def tidy_zh(text: str) -> str:
    text = re.sub(r"\.{3,}|。{3,}|[…⋯]+", "……", text)
    text = re.sub(r"(?<!—)—(?!—)|—{3,}", "——", text)
    text = re.sub(rf"(?<={_CJK})\?|\?(?={_CJK})", "？", text)
    text = re.sub(rf"(?<={_CJK})!|!(?={_CJK})", "！", text)
    text = re.sub(rf"(?<={_CJK}),|,(?={_CJK})", "，", text)
    text = re.sub(rf"~(?=$|[\s{_CJK}，。！？])|(?<={_CJK})~", "～", text)
    text = re.sub(r"(?<=～)[。.]+", "", text)
    text = re.sub(rf"吗[。.](?=$|\s|[{_CLOSERS}])", "吗？", text)
    text = re.sub(r"、\s*$", "，", text)
    return text


def tidy_translation(text: str, target: str | None) -> str:
    """翻译后的整理：目前只处理中文译文。"""
    return tidy_zh(text) if is_zh(target) and text else text
