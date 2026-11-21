"""强制对齐（Forced Alignment）接口。

对齐只用来校准词的时间戳，**不修改文本**，也不能作为文本正确性的依据。
目前只提供不做任何处理的默认实现；以后可以在这里接入基于声学模型的对齐后端
（主要用于纯文本模式，或时间戳精度不够的识别后端）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from ..asr.types import Word


class AlignError(RuntimeError):
    pass


class Aligner(Protocol):
    name: str

    def align(self, audio: Path, words: list[Word], language: str | None = None) -> list[Word]:
        """返回与输入一一对应、文本不变、时间已校准的词。"""
        ...


class NoopAligner:
    name = "none"

    def align(self, audio: Path, words: list[Word], language: str | None = None) -> list[Word]:
        return words


def create_aligner(cfg: dict[str, Any]) -> Aligner:
    backend = cfg.get("backend", "none")
    if backend == "none":
        return NoopAligner()
    raise AlignError(f"未知的对齐后端：{backend}")


def check_alignment(before: list[Word], after: list[Word]) -> None:
    """对齐结果必须与输入一一对应、文本不变。"""
    if len(before) != len(after) or any(a.text != b.text for a, b in zip(before, after)):
        raise AlignError("对齐后端修改了文本或词数，已拒绝其结果")
