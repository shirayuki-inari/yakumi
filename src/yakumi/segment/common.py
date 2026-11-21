"""断句的公共部分：句子数据、文本拼接、句末判断、超长句拆分。

断句结果用词区间表示：句子 k 覆盖 words[i:j]，所有区间首尾相接、覆盖全部词。
这样不论断句来自模型还是规则，文字和时间戳都只来自词本身，不会被改动。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..asr.types import Word
from ..text import CJK_RE, join_texts

__all__ = ["Sentence", "Span", "SENTENCE_END", "CLAUSE_END", "join_texts", "ends_sentence",
           "ends_clause", "gap_after", "text_len", "tokenize", "check_spans", "cap_spans"]

SENTENCE_END = set(".!?。！？…‼⁉")
CLAUSE_END = set(",;:，；：、")
_CLOSERS = "\"'”’」』）)】]〉》"

Span = tuple[int, int]


@dataclass
class Sentence:
    id: int
    start: float
    end: float
    text: str
    word_range: list[int] | None  # [i, j)；纯文本模式没有词，为 None
    source: str  # llm | rules

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ends_sentence(text: str) -> bool:
    return text.rstrip().rstrip(_CLOSERS)[-1:] in SENTENCE_END


def ends_clause(text: str) -> bool:
    return text.rstrip().rstrip(_CLOSERS)[-1:] in CLAUSE_END


def gap_after(words: list[Word], k: int) -> float:
    """第 k 个词与下一个词之间的停顿（秒）；最后一个词之后视为无限长。"""
    if k + 1 >= len(words):
        return float("inf")
    return words[k + 1].start - words[k].end


def text_len(words: list[Word], i: int, j: int) -> int:
    return sum(len(w.text) for w in words[i:j])


def tokenize(text: str) -> list[Word]:
    """纯文本模式：把文本切成“词”（中日韩文字逐字，其他按空格），没有时间信息。"""
    tokens: list[Word] = []
    for chunk in text.split():
        if CJK_RE.search(chunk):
            buf = ""
            for ch in chunk:
                if CJK_RE.match(ch) and buf and CJK_RE.search(buf):
                    tokens.append(Word(0.0, 0.0, buf))
                    buf = ""
                buf += ch
            if buf:
                tokens.append(Word(0.0, 0.0, buf))
        else:
            tokens.append(Word(0.0, 0.0, chunk))
    return tokens


def check_spans(spans: list[Span], n: int) -> None:
    """不变量：区间首尾相接、非空、覆盖 [0, n)。"""
    pos = 0
    for i, j in spans:
        if i != pos or j <= i:
            raise ValueError(f"断句区间不连续：{spans[:5]}…")
        pos = j
    if pos != n:
        raise ValueError(f"断句区间没有覆盖全部词：{pos} / {n}")


def cap_spans(words: list[Word], spans: list[Span], max_chars: int) -> list[Span]:
    """把超过 max_chars 的句子在停顿最长（其次是分句标点）的词边界处拆开，直到都不超长。"""
    out: list[Span] = []
    stack = list(reversed(spans))
    while stack:
        i, j = stack.pop()
        if j - i < 2 or text_len(words, i, j) <= max_chars:
            out.append((i, j))
            continue
        total = text_len(words, i, j)
        best, best_score = None, None
        done = 0
        for k in range(i, j - 1):
            done += len(words[k].text)
            if not (0.2 * total <= done <= 0.8 * total):
                continue
            score = (ends_sentence(words[k].text), ends_clause(words[k].text), round(gap_after(words, k), 1),
                     -abs(done - total / 2))
            if best_score is None or score > best_score:
                best, best_score = k + 1, score
        if best is None:
            best = (i + j) // 2
        stack.extend([(best, j), (i, best)])
    return out
