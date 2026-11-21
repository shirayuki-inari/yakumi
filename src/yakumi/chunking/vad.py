"""语音活动检测（VAD）。

VAD 只用来挑选切点和辅助发现漏字，**不会删除任何音频**：窗口始终覆盖连续的原始音频。
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any, Protocol

from .. import media
from ..files import cache_dir

Region = tuple[float, float]


class VAD(Protocol):
    name: str

    def speech_regions(self, audio: Path) -> list[Region]:
        """返回按时间排序、互不重叠的语音区间（绝对秒数）。"""
        ...


class NoVAD:
    """不检测：把整段视为语音，切分全部退化为强制切点。"""

    name = "none"

    def speech_regions(self, audio: Path) -> list[Region]:
        return [(0.0, media.probe_duration(audio))]


_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


class EnergyVAD:
    """基于 ffmpeg silencedetect 的能量检测。不需要额外依赖，但背景音乐会让它失效。"""

    name = "energy"

    def __init__(self, noise_db: float = -35.0, min_silence: float = 0.25):
        self.noise_db = noise_db
        self.min_silence = min_silence

    def speech_regions(self, audio: Path) -> list[Region]:
        duration = media.probe_duration(audio)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise media.MediaError("找不到 ffmpeg")
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-i", str(audio), "-vn",
             "-af", f"silencedetect=noise={self.noise_db}dB:d={self.min_silence}", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise media.MediaError("静音检测失败：" + proc.stderr.strip()[-300:])
        silences = parse_silencedetect(proc.stderr, duration)
        return complement(silences, duration)


def parse_silencedetect(log: str, duration: float) -> list[Region]:
    silences: list[Region] = []
    start: float | None = None
    for line in log.splitlines():
        if (m := _SILENCE_START.search(line)):
            start = max(0.0, float(m.group(1)))
        elif (m := _SILENCE_END.search(line)) and start is not None:
            silences.append((start, min(duration, float(m.group(1)))))
            start = None
    if start is not None:
        silences.append((start, duration))
    return silences


def complement(regions: list[Region], duration: float, min_len: float = 0.05) -> list[Region]:
    """[0, duration] 去掉 regions 后剩下的区间（忽略短于 min_len 的碎片）。"""
    out: list[Region] = []
    cursor = 0.0
    for s, e in sorted(regions):
        if s - cursor >= min_len:
            out.append((cursor, s))
        cursor = max(cursor, e)
    if duration - cursor >= min_len:
        out.append((cursor, duration))
    return out


SILERO_VERSION = "v6.2.3"
SILERO_URL = f"https://raw.githubusercontent.com/snakers4/silero-vad/{SILERO_VERSION}/src/silero_vad/data/silero_vad.onnx"
SILERO_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
SAMPLE_RATE = 16000
FRAME = 512  # 16kHz 下每帧 512 个采样（32 毫秒）
CONTEXT = 64  # 每帧前拼接上一帧末尾的 64 个采样


def model_cache_dir() -> Path:
    return cache_dir() / "models"


def ensure_silero_model() -> Path:
    """首次使用时下载 Silero VAD 模型（MIT 许可），校验 sha256 后缓存。"""
    path = model_cache_dir() / f"silero_vad_{SILERO_VERSION}.onnx"
    if path.is_file() and _sha256(path) == SILERO_SHA256:
        return path
    import httpx

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = httpx.get(SILERO_URL, timeout=60, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise media.MediaError(f"下载 Silero VAD 模型失败：{exc}") from exc
    if hashlib.sha256(resp.content).hexdigest() != SILERO_SHA256:
        raise media.MediaError("Silero VAD 模型校验失败，已放弃使用")
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(resp.content)
    os.replace(tmp, path)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def silero_available() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("numpy", "onnxruntime"))


class SileroVAD:
    """Silero VAD（ONNX）。对背景音乐的鲁棒性远好于能量检测。

    迟滞判定：概率 ≥ threshold 进入语音，连续 min_silence 秒低于 threshold − 0.15 才退出。
    不丢弃短促语音（与官方默认不同），因为这里只用来挑选切点。
    """

    name = "silero"

    def __init__(self, threshold: float = 0.5, min_silence: float = 0.1, model_path: Path | None = None):
        self.threshold = threshold
        self.neg_threshold = max(threshold - 0.15, 0.01)
        self.min_silence = min_silence
        self.model_path = model_path

    def _session(self):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        path = self.model_path or ensure_silero_model()
        return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])

    def probabilities(self, audio: Path) -> list[float]:
        import numpy as np

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            session = self._session()
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise media.MediaError("找不到 ffmpeg")
        proc = subprocess.Popen(
            [ffmpeg, "-v", "error", "-i", str(audio), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
             "-f", "s16le", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        state = np.zeros((2, 1, 128), dtype=np.float32)
        context = np.zeros((1, CONTEXT), dtype=np.float32)
        sr = np.array(SAMPLE_RATE, dtype=np.int64)
        probs: list[float] = []
        block = FRAME * 2 * 1024  # 每次读约 33 秒
        pending = b""
        assert proc.stdout is not None
        while True:
            data = proc.stdout.read(block)
            if not data:
                break
            pending += data
            usable = len(pending) - len(pending) % (FRAME * 2)
            samples = np.frombuffer(pending[:usable], dtype="<i2").astype(np.float32) / 32768.0
            pending = pending[usable:]
            for frame in samples.reshape(-1, FRAME):
                x = np.concatenate([context, frame[None, :]], axis=1)
                out, state = session.run(None, {"input": x, "state": state, "sr": sr})
                context = x[:, -CONTEXT:]
                probs.append(float(out[0][0]))
        if pending:
            frame = np.zeros(FRAME, dtype=np.float32)
            tail = np.frombuffer(pending[: len(pending) - len(pending) % 2], dtype="<i2")
            frame[: len(tail)] = tail.astype(np.float32) / 32768.0
            x = np.concatenate([context, frame[None, :]], axis=1)
            out, state = session.run(None, {"input": x, "state": state, "sr": sr})
            probs.append(float(out[0][0]))
        _, err = proc.communicate()
        if proc.returncode != 0:
            raise media.MediaError("解码音频失败：" + err.decode(errors="replace").strip()[-300:])
        return probs

    def speech_regions(self, audio: Path) -> list[Region]:
        return regions_from_probs(self.probabilities(audio), FRAME / SAMPLE_RATE,
                                  self.threshold, self.neg_threshold, self.min_silence)


def regions_from_probs(probs: list[float], frame_sec: float, threshold: float, neg_threshold: float,
                       min_silence: float) -> list[Region]:
    regions: list[Region] = []
    start: float | None = None
    silent_since: float | None = None
    for i, p in enumerate(probs):
        t = i * frame_sec
        if start is None:
            if p >= threshold:
                start, silent_since = t, None
        elif p < neg_threshold:
            silent_since = t if silent_since is None else silent_since
            if t + frame_sec - silent_since >= min_silence:
                regions.append((round(start, 3), round(silent_since, 3)))
                start, silent_since = None, None
        else:
            silent_since = None
    if start is not None:
        regions.append((round(start, 3), round(len(probs) * frame_sec, 3)))
    return regions


def create_vad(cfg: dict[str, Any]) -> VAD:
    kind = cfg.get("vad", "silero")
    if kind == "none":
        return NoVAD()
    if kind == "silero":
        if silero_available():
            return SileroVAD(threshold=float(cfg.get("vad_threshold", 0.5)))
        print("提示：没有安装 onnxruntime，改用能量检测（背景音乐较强时切点可能不准）。"
              "Termux 可执行 pkg install python-onnxruntime。", file=sys.stderr)
    return EnergyVAD(noise_db=float(cfg.get("silence_db", -35)))
