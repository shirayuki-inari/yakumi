"""文本工具：中日韩文字判断、按语言拼接、每条字幕的字数上限。

中日韩文字之间不加空格，拉丁文字之间加一个空格。识别结果、断句、合并和复核都按这个规则拼接词。
"""

from __future__ import annotations

import re

# 平假名、片假名、中日韩统一表意文字（含扩展 A）、韩文音节、兼容表意文字
CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]")
MAX_LINES = 2  # 每条字幕最多两行


def is_cjk(text: str) -> bool:
    """非空白字符里中日韩文字超过 30% 时视为中日韩文本。"""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return False
    return len(CJK_RE.findall(text)) / len(chars) > 0.3


def char_limit(text: str, max_chars: int, max_chars_cjk: int) -> int:
    """一条字幕（最多两行）能容纳的字数。"""
    return (max_chars_cjk if is_cjk(text) else max_chars) * MAX_LINES


def join_text(a: str, b: str) -> str:
    """拼接两段文字：任一侧紧挨着中日韩文字时直接相连，否则加一个空格。"""
    if not a:
        return b
    if not b:
        return a
    if is_cjk(a[-1]) or is_cjk(b[0]):
        return a + b
    return f"{a} {b}"


def join_texts(texts: list[str]) -> str:
    out = ""
    for t in texts:
        out = join_text(out, t)
    return out
