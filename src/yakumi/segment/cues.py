"""句子 → 字幕条：在每个句子内部按显示需要拆分，从不跨句。

- 词之间停顿 ≥ cue_gap 时拆开：说话停了，字幕也先消失；
- 超过两行字数或 max_duration 时拆开：优先在分句标点处，其次在停顿最长处，再其次在中点附近；
- 很短的碎片（中日韩 ≤ 3 字、其他 ≤ 8 个字符）在句内与相邻字幕条合并，停顿不太长时才合并；
- 显示时间不足 min_duration 的字幕条，在不碰到下一条的前提下延长显示。
"""

from __future__ import annotations

from ..asr.types import Word
from ..models import Segment
from ..text import CJK_RE, char_limit, is_cjk, join_texts
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
    """去掉重叠，保证时长为正；过短的字幕条延长到 min_duration，不与相邻字幕条重叠。

    先往后延长到下一条开始为止；仍然不够时（识别给出的时间戳重叠或挤在一起，
    例如这一条和下一条的开始时间相同），提前到上一条结束后的空档里，
    再不够才推迟下一条的开始（下一条自己至少保留 min_duration）。
    """
    starts: list[int] = []
    for cue in cues:
        earliest = starts[-1] + 1 if starts else 0
        starts.append(max(earliest, round(cue.start * 1000)))
    ends = [max(round(cue.end * 1000), start + 1) for cue, start in zip(cues, starts)]
    minimum = max(1, round(min_duration * 1000))
    prev_end = 0
    for idx, cue in enumerate(cues):
        start = starts[idx]
        end = max(ends[idx], start + minimum)
        has_next = idx + 1 < len(starts)
        if has_next:
            end = min(end, starts[idx + 1])
        if end - start < minimum:
            start = max(prev_end, min(start, end - minimum))
        if end - start < minimum and has_next:
            nxt = idx + 1
            room = ends[nxt] - minimum - starts[nxt]  # 下一条保留 min_duration 后多出的时间
            if nxt + 1 < len(starts):
                room = min(room, starts[nxt + 1] - 1 - starts[nxt])
            delay = max(0, min(minimum - (end - start), room))
            starts[nxt] += delay
            end += delay
        cue.start, cue.end = start / 1000, end / 1000
        prev_end = end
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

    spaces = []
    cjk_boundaries = []
    for i in range(1, n):
        left, right = text[i - 1], text[i]
        if right == " " and i + 1 < n and not text[i + 1].isspace() and not left.isspace():
            if not CJK_RE.search(left + text[i + 1]):
                spaces.append(i)
        elif not left.isspace() and not right.isspace() and CJK_RE.search(left + right):
            cjk_boundaries.append(i)
    allowed = spaces + cjk_boundaries
    for marks in (SENTENCE_END, CLAUSE_END):
        cands = [i for i in allowed if text[i - 1] in marks]
        # 只接受不太偏的切点，避免切出一两个字的碎片
        point = best([c for c in cands if n * 0.2 <= c <= n * 0.8])
        if point:
            return point
    return best(spaces) or best(cjk_boundaries)


def split_by_ratio(seg: Segment, max_duration: float, max_chars: int, max_chars_cjk: int) -> list[Segment]:
    """没有词级时间时拆分超长的字幕条：在标点或空格处切开，时间按字数比例分配。"""
    text = seg.text
    duration = seg.end - seg.start
    if (len(text) <= char_limit(text, max_chars, max_chars_cjk) and duration <= max_duration) or len(text) < 4:
        return [seg]
    point = _text_split_point(text)
    if point is None:
        return [seg]
    left, right = text[:point].strip(), text[point:].strip()
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
