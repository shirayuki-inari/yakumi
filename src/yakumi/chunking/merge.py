"""合并各窗口的识别结果。

词级模式（词已换算成全局时间）：
- 先清理时间戳的小异常（end < start、时长为 0）。
- 每个切点两侧都有重叠（VAD 判定的“静音”不一定真的没人说话），两个窗口在重叠区里
  各识别了一遍同一段音频。把两边的词拆成比较单元
  （拉丁文字按词、去掉大小写和标点；中日韩文字按字），在**时间约束**下求 LCS：
  两个单元只有在时间相差不超过 match_tolerance 时才算匹配。拼接点从两边都恰好落在词尾的
  匹配对中选：保留内容最多的优先（一侧漏词时自动取另一侧的版本），其次离切点最近。
  A 保留到该词为止，B 从下一个词开始。
  - 只在相邻窗口的重叠区内去重，所以同一窗口内的口头重复（“no no no”）、
    不同时间说的同一句话都会保留。
  - 两次识别的结果可能不完全一样（同音字、分词粒度不同），LCS 允许部分不匹配。
  - 没有可用的匹配时退回按中点归属。重叠区里确实有词却对不上，才标记为高风险交给边界修复；
    重叠区里没有词（真的没人说话）时按中点归属是安全的，不算风险。
  - 片段边缘 edge_margin 秒内（A 片段末尾、B 片段开头）另一侧往往只听到半个音，
    那里对不上不作为风险依据；只看两个片段都可靠的区间。按中点归属时它们本来就不会被保留（A 的边缘在切点之后，B 的在切点之前）。

纯文本模式：仅在相邻窗口有重叠时匹配 A 的后缀与 B 的前缀；不确定时保留原文。
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Sequence

from ..asr.types import AudioWindow, Word
from ..text import CJK_RE

MAX_ZERO_FIX = 0.1  # 时长为 0 的词最多向后扩展这么多


@dataclass
class TextBlock:
    core_start: float
    core_end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"core_start": self.core_start, "core_end": self.core_end, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TextBlock":
        return cls(float(d["core_start"]), float(d["core_end"]), str(d["text"]))


@dataclass
class BoundaryReport:
    time: float
    kind: str
    strategy: str  # lcs | empty（重叠区没有词）| edge（只有片段边缘的残音）| midpoint | text_overlap | concat
    matches: int = 0
    risky: bool = False

    def to_dict(self) -> dict[str, Any]:
        return vars(self).copy()


# ---- 词的清理与规范化 ------------------------------------------------------
def clean_words(words: list[Word]) -> list[Word]:
    """修正 end < start、时长为 0 等小异常，并按开始时间排序（稳定排序，不打乱同时刻的词）。"""
    fixed = []
    for w in words:
        start, end = (w.start, w.end) if w.end >= w.start else (w.end, w.start)
        fixed.append(Word(max(0.0, start), max(0.0, end), w.text, w.confidence))
    fixed.sort(key=lambda w: w.start)
    for i, w in enumerate(fixed):
        if w.end <= w.start:
            limit = fixed[i + 1].start if i + 1 < len(fixed) else w.start + MAX_ZERO_FIX
            w.end = round(w.start + max(0.0, min(MAX_ZERO_FIX, limit - w.start)), 3)
    return fixed


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in text if ch.isalnum())


@dataclass
class Unit:
    key: str
    time: float
    word: int  # 所属词在列表中的下标
    last: bool  # 是否是该词的最后一个单元


def to_units(words: list[Word]) -> list[Unit]:
    units: list[Unit] = []
    for i, w in enumerate(words):
        key = _norm(w.text)
        if not key:
            continue
        if CJK_RE.search(key):
            n = len(key)
            for k, ch in enumerate(key):
                t = w.start + (k + 0.5) / n * (w.end - w.start)
                units.append(Unit(ch, t, i, k == n - 1))
        else:
            units.append(Unit(key, (w.start + w.end) / 2, i, True))
    return units


def timed_lcs(a: list[Unit], b: list[Unit], tolerance: float) -> list[tuple[int, int]]:
    """时间约束下的最长公共子序列，返回匹配的 (a 下标, b 下标)。"""
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if a[i].key == b[j].key and abs(a[i].time - b[j].time) <= tolerance:
                dp[i][j] = dp[i + 1][j + 1] + 1
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
    pairs = []
    i = j = 0
    while i < n and j < m:
        if a[i].key == b[j].key and abs(a[i].time - b[j].time) <= tolerance and dp[i][j] == dp[i + 1][j + 1] + 1:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


# ---- 词级合并 ---------------------------------------------------------------
def midpoint(w: Word) -> float:
    return (w.start + w.end) / 2


def splice(a_words: list[Word], b_words: list[Word], a_win: AudioWindow, b_win: AudioWindow,
           tolerance: float, edge_margin: float = 0.5) -> tuple[Sequence[int], Sequence[int], BoundaryReport]:
    """在切点处拼接相邻窗口（两个窗口在切点两侧有重叠）。

    返回两侧要保留的词下标和边界报告。中点归属可能保留非连续的词。
    """
    cut = a_win.core_end
    kind = a_win.right or "forced"
    lo, hi = b_win.start, a_win.end  # 两个窗口都覆盖的区间
    a_idx = [i for i, w in enumerate(a_words) if lo <= midpoint(w) <= hi]
    b_idx = [i for i, w in enumerate(b_words) if lo <= midpoint(w) <= hi]
    a_units = to_units([a_words[i] for i in a_idx])
    b_units = to_units([b_words[i] for i in b_idx])
    pairs = timed_lcs(a_units, b_units, tolerance)

    aligned = [(i, j) for i, j in pairs if a_units[i].last and b_units[j].last]
    if aligned:
        # 拼接后重叠区的内容 = A 的前 i+1 个单元 + B 第 j 个之后的单元。
        # 选保留内容最多的拼接点：一侧漏了词（例如切点附近识别不全）时，自动取另一侧的版本；
        # 内容一样多时取离切点最近的。
        nb = len(b_units)
        i, j = max(aligned, key=lambda p: (p[0] + 1 + nb - p[1] - 1, -abs(a_units[p[0]].time - cut)))
        a_end = a_idx[a_units[i].word] + 1
        b_start = b_idx[b_units[j].word] + 1
        return range(a_end), range(b_start, len(b_words)), BoundaryReport(cut, kind, "lcs", len(pairs))

    # 没有可用的匹配：按中点归属。重叠区里有（片段边缘以外的）词却对不上才算高风险，交给边界修复
    keep_a = [i for i, w in enumerate(a_words) if midpoint(w) < cut]
    keep_b = [i for i, w in enumerate(b_words) if midpoint(w) >= cut]
    # 只有在两个片段都可靠的区间内（B 开头之后、A 结尾之前各留 edge_margin）对不上，才算风险
    lo_ok, hi_ok = b_win.start + edge_margin, a_win.end - edge_margin
    inner = [w for w in [a_words[i] for i in a_idx] + [b_words[i] for i in b_idx]
             if lo_ok <= midpoint(w) <= hi_ok and to_units([w])]
    if inner:
        strategy, risky = "midpoint", True
    else:
        strategy, risky = ("edge" if a_units or b_units else "empty"), False
    return keep_a, keep_b, BoundaryReport(cut, kind, strategy, len(pairs), risky=risky)


def merge_words(pieces: list[tuple[AudioWindow, list[Word]]], tolerance: float = 0.5,
                edge_margin: float = 0.5) -> tuple[list[Word], list[BoundaryReport]]:
    """pieces 中的词为全局时间。返回合并后的词和每个边界的报告。"""
    pieces = sorted(((w, clean_words(ws)) for w, ws in pieces), key=lambda p: p[0].core_start)
    n = len(pieces)
    kept = [set(range(len(ws))) for _, ws in pieces]
    reports: list[BoundaryReport] = []
    for k in range(n - 1):
        (a_win, a_words), (b_win, b_words) = pieces[k], pieces[k + 1]
        left, right, report = splice(a_words, b_words, a_win, b_win, tolerance, edge_margin)
        kept[k].intersection_update(left)
        kept[k + 1].intersection_update(right)
        reports.append(report)

    merged = [word for (_, words), indices in zip(pieces, kept) for i, word in enumerate(words) if i in indices]
    return clean_words(merged), reports


# ---- 纯文本合并 -------------------------------------------------------------
_SPACE_RE = re.compile(r"\s+")
# 用宽松的语速上限约束可能来自重叠区的字符数。
_TEXT_CHARS_PER_SECOND = 20


def _norm_with_map(text: str) -> tuple[str, list[int]]:
    """规范化文本，并记录每个规范化字符在原文中的位置。"""
    chars, index = [], []
    for i, ch in enumerate(text):
        n = _norm(ch)
        for c in n:
            chars.append(c)
            index.append(i)
    return "".join(chars), index


def dedupe_text(a: str, b: str, min_match: int = 4, window: int = 80) -> tuple[str, bool]:
    """只在后缀和前缀完全匹配、且落在文字边界时去重。"""
    a_norm, a_map = _norm_with_map(a)
    b_norm, b_map = _norm_with_map(b)
    for size in range(min(window, len(a_norm), len(b_norm)), max(1, min_match) - 1, -1):
        if a_norm[-size:] != b_norm[:size]:
            continue
        start = len(a_norm) - size
        if (start > 0 and a_map[start - 1] == a_map[start]) or (size < len(b_map) and b_map[size - 1] == b_map[size]):
            continue  # 规范化后扩展成多个字符的原字符也不能被切开
        cut = b_map[size - 1] + 1
        if _text_boundary(a, a_map[start]) and _text_boundary(b, cut):
            return b[cut:].lstrip(" ,，、.。!！?？"), True
    return b, False


def _text_boundary(text: str, cut: int) -> bool:
    if cut == 0 or cut == len(text):
        return True
    left, right = text[cut - 1], text[cut]
    return not (left.isalnum() and right.isalnum()) or bool(CJK_RE.search(left + right))


def merge_text(pieces: list[tuple[AudioWindow, str]]) -> tuple[list[TextBlock], list[BoundaryReport]]:
    pieces = sorted(pieces, key=lambda p: p[0].core_start)
    blocks: list[TextBlock] = []
    reports: list[BoundaryReport] = []
    previous: AudioWindow | None = None
    prev_text = ""
    for w, raw in pieces:
        text = _SPACE_RE.sub(" ", raw).strip()
        if previous is not None:
            overlap = min(previous.end, w.end) - max(previous.start, w.start)
            found = False
            if overlap > 0:
                limit = min(80, math.ceil(overlap * _TEXT_CHARS_PER_SECOND))
                text, found = dedupe_text(prev_text, text, window=limit)
            kind = w.left or previous.right or "silence"
            uncertain = overlap > 0 and bool(prev_text and raw.strip()) and not found
            reports.append(BoundaryReport(w.core_start, kind, "text_overlap" if found else "concat",
                                          risky=uncertain or (not found and kind == "forced")))
        if text:
            blocks.append(TextBlock(w.core_start, w.core_end, text))
        previous, prev_text = w, text
    return blocks, reports
