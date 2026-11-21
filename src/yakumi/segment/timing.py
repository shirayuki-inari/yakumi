"""纯文本模式的时间估算：没有词级时间戳时，把句子按字数比例铺到语音区间上。"""

from __future__ import annotations

from ..models import Segment


def place_sentences(sentences: list[str], core: tuple[float, float],
                    speech: list[tuple[float, float]]) -> list[Segment]:
    """把句子按字符数比例铺到窗口 core 内的语音区间上。"""
    start, end = core
    regions = [(max(s, start), min(e, end)) for s, e in speech if e > start and s < end]
    regions = [(s, e) for s, e in regions if e > s] or [(start, end)]
    total = sum(e - s for s, e in regions)
    n_chars = sum(len(s) for s in sentences)
    if not sentences or n_chars == 0 or total <= 0:
        return []

    def at(offset: float) -> float:
        """语音时间轴上的偏移 → 绝对时间。"""
        for s, e in regions:
            if offset <= e - s:
                return s + offset
            offset -= e - s
        return regions[-1][1]

    segments = []
    done = 0
    for sentence in sentences:
        a = total * done / n_chars
        done += len(sentence)
        b = total * done / n_chars
        segments.append(Segment(round(at(a), 3), round(at(b), 3), sentence))
    return segments
