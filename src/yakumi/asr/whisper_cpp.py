"""本地识别：调用 whisper.cpp 的命令行程序 whisper-cli。

需要先编译 whisper.cpp 并下载 ggml 模型，见 README。whisper.cpp 自己处理长音频，
没有超时和大小限制，所以整段识别、不切分。
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .. import media
from ..models import Segment
from .base import ASRError
from .types import INF, ASRCapabilities, AudioWindow, RawTranscript


class WhisperCppASR:
    name = "whisper_cpp"

    def __init__(self, binary: str, model: str, threads: int = 4):
        self.binary = str(Path(binary).expanduser())
        self.model = str(Path(model).expanduser()) if model else ""
        self.threads = threads
        self.mode = "text"  # 词级时间戳不可用，分段能力通过 capabilities 声明

    def capabilities(self) -> ASRCapabilities:
        return ASRCapabilities(word_timestamps=False, max_window=INF, target_window=INF, segment_timestamps=True)

    def check(self) -> None:
        if not (shutil.which(self.binary) or Path(self.binary).is_file()):
            raise ASRError(f"找不到 whisper.cpp 程序：{self.binary}")
        if not self.model or not Path(self.model).is_file():
            raise ASRError(f"找不到 whisper.cpp 模型文件：{self.model or '(未配置)'}")

    def transcribe(self, clip: Path, language: str | None = None,
                   window: AudioWindow | None = None) -> RawTranscript:
        self.check()
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="yakumi-wcpp-") as tmp:
            wav = media.extract_wav(clip, Path(tmp) / "audio.wav")
            out_base = Path(tmp) / "out"
            args = [self.binary, "-m", self.model, "-f", str(wav), "-oj", "-of", str(out_base),
                    "-t", str(self.threads), "-l", language or "auto"]
            proc = subprocess.run(args, capture_output=True, text=True)
            if proc.returncode != 0:
                raise ASRError("whisper.cpp 执行失败：" + proc.stderr.strip()[-500:])
            out = out_base.with_suffix(".json")
            if not out.is_file():
                raise ASRError("whisper.cpp 没有输出 JSON 结果")
            try:
                body = json.loads(out.read_text(encoding="utf-8", errors="replace"))
            except ValueError as exc:
                raise ASRError(f"whisper.cpp 输出的 JSON 无法解析：{exc}") from exc

        segments = []
        try:
            items = body.get("transcription", [])
            if not isinstance(items, list):
                raise ValueError("transcription 必须是列表")
            for item in items:
                text = item.get("text", "").strip()
                if not text:
                    continue
                offsets = item["offsets"]
                start, end = float(offsets["from"]) / 1000, float(offsets["to"]) / 1000
                if not 0 <= start < end or not math.isfinite(end):
                    raise ValueError("分段时间无效")
                segments.append(Segment(start, end, text))
            lang = (body.get("result") or {}).get("language")
            if lang is not None and not isinstance(lang, str):
                raise ValueError("语言字段格式不正确")
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ASRError(f"whisper.cpp 输出的识别结果格式不正确：{exc}") from exc
        return RawTranscript(text=" ".join(s.text for s in segments), segments=segments, language=lang,
                             elapsed=time.monotonic() - t0)
