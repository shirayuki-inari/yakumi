"""规则断句（LLM 断句失败时的兜底，也可以在配置里直接指定）。

只在句末标点处断句；停顿只在很长（默认 ≥ 3 秒）时才视为句子边界。
较短的停顿不再断句，只用来把句子拆成多条字幕显示（见 cues.py）。
没有标点的长段落按 cap_spans 拆开，避免一句话太长。
"""

from __future__ import annotations

from ..asr.types import Word
from .common import Span, cap_spans, ends_sentence, gap_after

LONG_PAUSE = 3.0


def rule_spans(words: list[Word], max_chars: int, long_pause: float = LONG_PAUSE,
               start: int = 0, end: int | None = None) -> list[Span]:
    end = len(words) if end is None else end
    spans: list[Span] = []
    first = start
    for k in range(start, end):
        if k == end - 1 or ends_sentence(words[k].text) or gap_after(words, k) >= long_pause:
            spans.append((first, k + 1))
            first = k + 1
    return cap_spans(words, spans, max_chars)
