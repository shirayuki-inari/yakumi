"""测试与调试用：不调用任何服务，生成可复现的识别结果。

按**绝对时间**每 0.5 秒生成一个词 `w{n}`（n = 该词起点 × 2），所以不同窗口对同一段音频
给出完全相同的词，可以用来验证切分与合并：最终词序列应当恰好是 w0, w1, w2, …。
没有传入窗口信息时，以片段起点为 0。
"""

from __future__ import annotations

import math
from pathlib import Path

from .. import media
from ..models import Segment
from .base import ASRTimeout
from .types import INF, ASRCapabilities, AudioWindow, RawTranscript, Word

STEP = 0.5


class FakeASR:
    name = "fake"

    def __init__(self, mode: str = "words", target_window: float = 300.0, max_window: float = 480.0,
                 timeout_over: float = INF):
        self.word_mode = mode != "text"
        self.mode = "text" if mode == "text" else "words"
        self.target_window = target_window
        self.max_window = max_window
        self.timeout_over = timeout_over  # 片段超过该时长时模拟超时
        self.calls: list[tuple[float, float]] = []

    def capabilities(self) -> ASRCapabilities:
        return ASRCapabilities(word_timestamps=self.word_mode, max_window=self.max_window,
                               target_window=self.target_window, timeout=None)

    def transcribe(self, clip: Path, language: str | None = None,
                   window: AudioWindow | None = None) -> RawTranscript:
        duration = media.probe_duration(clip)
        offset = window.start if window else 0.0
        self.calls.append((offset, offset + duration))
        if duration > self.timeout_over:
            raise ASRTimeout(f"模拟超时：片段 {duration:.1f} 秒")
        words = []
        n = math.ceil(offset / STEP - 1e-6)
        while n * STEP + STEP * 0.8 <= offset + duration + 1e-6:
            start = n * STEP - offset
            words.append(Word(start, start + STEP * 0.8, f"w{n}"))
            n += 1
        text = " ".join(w.text for w in words)
        if self.word_mode:
            return RawTranscript(text=text, words=words, segments=[Segment(0.0, duration, text)],
                                 language=language or "en", cost=0.0)
        return RawTranscript(text=text, language=language or "en", cost=0.0)
