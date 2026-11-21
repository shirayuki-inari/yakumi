from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .types import ASRCapabilities, AudioWindow, RawTranscript


class ASRError(RuntimeError):
    pass


class ASRTimeout(ASRError):
    """请求超时。调用方应把窗口拆小后重试。"""


class ASRTooLarge(ASRError):
    """片段超出后端的大小或时长限制。调用方应把窗口拆小后重试。"""


class ASRProvider(Protocol):
    name: str
    # 输出模式：words（词级时间戳）| text（纯文本）| auto（首次使用时探测，结果写回这个属性）
    mode: str

    def capabilities(self) -> ASRCapabilities: ...

    def transcribe(self, clip: Path, language: str | None = None,
                   window: AudioWindow | None = None) -> RawTranscript:
        """识别一个片段，返回片段内的局部时间。

        `window` 仅供参考（例如假后端据此生成可复现的结果），真实后端不依赖它。
        """
        ...
