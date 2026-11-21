"""句子 → 字幕条：在每个句子内部按显示需要拆分，从不跨句。

- 词之间停顿 ≥ cue_gap 时拆开：说话停了，字幕也先消失；
- 超过两行字数或 max_duration 时拆开：优先在分句标点处，其次在停顿最长处，再其次在中点附近；
- 很短的碎片（中日韩 ≤ 3 字、其他 ≤ 8 个字符）在句内与相邻字幕条合并，停顿不太长时才合并；
- 显示时间不足 min_duration 的字幕条，在不碰到下一条的前提下延长显示。
"""

from __future__ import annotations

from ..asr.types import Word
from ..models import Segment
from ..text import char_limit, is_cjk, join_texts
from .common import CLAUSE_END, SENTENCE_END, Span, ends_clause, ends_sentence, gap_after

TINY_CJK = 3
TINY_LATIN = 8
TINY_MAX_GAP = 3.0


def _is_tiny(text: str) -> bool:
    return len(text) <= (TINY_CJK if is_cjk(text) else TINY_LATIN)


def _split_long(words: list[Word], span: Span, max_chars: int, max_chars_cjk: int,
                max_duration: float) -> list[Span]:
    i, j = span
    text = join_texts([w.text for w in words[i:j]])
    duration = words[j - 1].end - words[i].start
    if j - i < 2 or (len(text) <= char_limit(text, max_chars, max_chars_cjk) and duration <= max_duration):
        return [span]
    total = sum(len(w.text) for w in words[i:j])
    best, best_key = None, None
    done = 0
    for k in range(i, j - 1):
        done += len(words[k].text)
        if not (0.2 * total <= done <= 0.8 * total):
            continue
        key = (ends_sentence(words[k].text) or ends_clause(words[k].text), round(gap_after(words, k), 1),
               -abs(done - total / 2))
        if best_key is None or key > best_key:
            best, best_key = k + 1, key
    if best is None:
        best = (i + j) // 2
    return (_split_long(words, (i, best), max_chars, max_chars_cjk, max_duration)
            + _split_long(words, (best, j), max_chars, max_chars_cjk, max_duration))


def _merge_tiny(words: list[Word], parts: list[Span], max_chars: int, max_chars_cjk: int,
                max_duration: float) -> list[Span]:
    def text(p: Span) -> str:
        return join_texts([w.text for w in words[p[0]:p[1]]])

    def fits(a: Span, b: Span) -> bool:
        joined = text((a[0], b[1]))
        return (len(joined) <= char_limit(joined, max_chars, max_chars_cjk)
                and words[b[1] - 1].end - words[a[0]].start <= max_duration
                and words[b[0]].start - words[a[1] - 1].end <= TINY_MAX_GAP)

    parts = list(parts)
    changed = True
    while changed and len(parts) > 1:
        changed = False
        for idx, part in enumerate(parts):
            if not _is_tiny(text(part)):
                continue
            options = []
            if idx > 0 and fits(parts[idx - 1], part):
                options.append((words[part[0]].start - words[parts[idx - 1][1] - 1].end, idx - 1))
            if idx + 1 < len(parts) and fits(part, parts[idx + 1]):
                options.append((words[parts[idx + 1][0]].start - words[part[1] - 1].end, idx + 1))
            if options:
                _, other = min(options)
                lo, hi = sorted((idx, other))
                parts[lo:hi + 1] = [(parts[lo][0], parts[hi][1])]
                changed = True
                break
    return parts


def sentence_cues(words: list[Word], span: Span, cue_gap: float, max_chars: int, max_chars_cjk: int,
                  max_duration: float) -> list[Span]:
    i, j = span
    parts: list[Span] = []
    first = i
    for k in range(i, j):
        if k == j - 1 or words[k + 1].start - words[k].end >= cue_gap:
            parts.append((first, k + 1))
            first = k + 1
    split: list[Span] = []
    for part in parts:
        split.extend(_split_long(words, part, max_chars, max_chars_cjk, max_duration))
    return _merge_tiny(words, split, max_chars, max_chars_cjk, max_duration)


def fix_timing(cues: list[Segment], min_duration: float) -> list[Segment]:
    """去掉重叠，保证时长为正；过短的字幕条在不碰到下一条的前提下延长到 min_duration。"""
    for idx, cue in enumerate(cues):
        nxt = cues[idx + 1].start if idx + 1 < len(cues) else None
        if nxt is not None and cue.end > nxt:
            cue.end = nxt
        if cue.end - cue.start < min_duration:
            want = cue.start + min_duration
            cue.end = min(want, nxt) if nxt is not None else want
        if cue.end <= cue.start:
            cue.end = cue.start + 0.1
        cue.start, cue.end = round(cue.start, 3), round(cue.end, 3)
    return cues


def build_cues(words: list[Word], spans: list[Span], cue_gap: float = 1.2, max_chars: int = 42,
               max_chars_cjk: int = 20, max_duration: float = 6.0, min_duration: float = 0.8) -> list[Segment]:
    cues: list[Segment] = []
    for gid, span in enumerate(spans):
        for a, b in sentence_cues(words, span, cue_gap, max_chars, max_chars_cjk, max_duration):
            cues.append(Segment(words[a].start, max(words[b - 1].end, words[a].start),
                                join_texts([w.text for w in words[a:b]]), group=gid))
    return fix_timing(cues, min_duration)


def _text_split_point(text: str) -> int | None:
    """纯文本切分点（切在该下标之前）：优先句末标点、其次分句标点、再其次空格，尽量靠近中间。"""
    n = len(text)
    if n < 2:
        return None
    mid = n / 2

    def best(cands: list[int]) -> int | None:
        cands = [c for c in cands if 0 < c < n]
        return min(cands, key=lambda c: abs(c - mid)) if cands else None

    for marks in (SENTENCE_END, CLAUSE_END):
        cands = [i + 1 for i, ch in enumerate(text[:-1]) if ch in marks]
        # 只接受不太偏的切点，避免切出一两个字的碎片
        point = best([c for c in cands if n * 0.2 <= c <= n * 0.8])
        if point:
            return point
    return best([i for i, ch in enumerate(text) if ch == " "]) or n // 2


def split_by_ratio(seg: Segment, max_duration: float, max_chars: int, max_chars_cjk: int) -> list[Segment]:
    """没有词级时间时拆分超长的字幕条：在标点或空格处切开，时间按字数比例分配。"""
    text = seg.text
    duration = seg.end - seg.start
    if (len(text) <= char_limit(text, max_chars, max_chars_cjk) and duration <= max_duration) or len(text) < 4:
        return [seg]
    point = _text_split_point(text)
    left, right = text[:point].strip(), text[point:].strip()
    if not point or not left or not right:
        return [seg]
    cut = seg.start + duration * len(left) / (len(left) + len(right))
    return (split_by_ratio(Segment(seg.start, cut, left), max_duration, max_chars, max_chars_cjk)
            + split_by_ratio(Segment(cut, seg.end, right), max_duration, max_chars, max_chars_cjk))


def text_cues(sentences: list[Segment], max_chars: int = 42, max_chars_cjk: int = 20,
              max_duration: float = 6.0, min_duration: float = 0.8) -> list[Segment]:
    """纯文本模式：句子的时间是估算的，只按字数和时长拆分。sentences 的 group 已设置好。"""
    cues: list[Segment] = []
    for sent in sentences:
        for piece in split_by_ratio(Segment(sent.start, sent.end, sent.text), max_duration, max_chars,
                                    max_chars_cjk):
            piece.group = sent.group
            cues.append(piece)
    return fix_timing(cues, min_duration)
