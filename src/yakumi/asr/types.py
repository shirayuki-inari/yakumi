"""识别相关的数据类型。

所有标注为“绝对时间”的字段都相对于原音频起点（秒）；
后端返回的 RawTranscript 使用片段内的局部时间，进入合并前统一换算。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..models import Segment

INF = float("inf")


@dataclass
class Word:
    start: float
    end: float
    text: str
    confidence: float | None = None

    def shifted(self, offset: float) -> "Word":
        return Word(self.start + offset, self.end + offset, self.text, self.confidence)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Word":
        return cls(float(d["start"]), float(d["end"]), str(d["text"]), d.get("confidence"))


@dataclass
class RawTranscript:
    """后端对一个片段的识别结果，时间为片段内的局部时间。"""

    text: str = ""
    words: list[Word] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    language: str | None = None
    cost: float | None = None  # 美元，由后端返回；不返回时为 None
    elapsed: float | None = None  # 请求耗时（秒）


@dataclass
class ASRCapabilities:
    word_timestamps: bool
    max_window: float = INF  # 单次请求的硬上限（秒）
    target_window: float = INF  # 词级模式的默认目标窗口
    timeout: float | None = None  # 服务端单次请求超时（秒）


@dataclass
class Boundary:
    """相邻窗口之间的切点（绝对时间）。"""

    time: float
    kind: str  # "silence" | "forced"
    overlap: float = 0.0  # forced 边界两侧各自的重叠长度
    gap: float = 0.0  # silence 边界所在静音的长度

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Boundary":
        return cls(**d)


@dataclass
class AudioWindow:
    """一次识别请求覆盖的音频区间（绝对时间）。

    [start, end] 是实际送去识别的范围（含两侧的重叠）；
    [core_start, core_end] 是该窗口“负责”的区间，所有窗口的 core 首尾相接、互不重叠。
    """

    index: int
    start: float
    end: float
    core_start: float
    core_end: float
    left: str | None = None  # 左侧边界类型：None（音频开头）| "silence" | "forced"
    right: str | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AudioWindow":
        return cls(**d)
