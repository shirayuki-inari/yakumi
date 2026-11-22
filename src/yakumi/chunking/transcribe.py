"""分窗识别：能力探测 → 规划窗口 → 逐窗识别（缓存、超时拆分、截断检测、费用统计）。

产物（都在任务目录下）：
    windows.json          当前的窗口计划（识别过程中可能因拆分而更新）
    pieces/window_NNNN.json  每个窗口的识别结果，词为全局时间
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .. import media
from ..files import cache_dir, write_json
from ..asr.base import ASRError, ASRProvider, ASRTimeout, ASRTooLarge
from ..asr.types import AudioWindow, Boundary, Word
from ..models import Segment
from ..text import join_texts
from .planner import PlanParams, initial_windows, params_for, plan_windows, split_window

Region = tuple[float, float]
Report = Callable[[float, str], None]
PROBE_SECONDS = 12.0
CAPABILITY_CACHE_DAYS = 7


def capability_cache_path() -> Path:
    return cache_dir() / "capabilities.json"


def _read_capability_cache() -> dict[str, Any]:
    try:
        data = json.loads(capability_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cached_capability(key: str) -> dict[str, Any] | None:
    entry = _read_capability_cache().get(key)
    if not isinstance(entry, dict) or entry.get("mode") not in ("words", "text"):
        return None
    if entry["mode"] == "text" and not entry.get("confirmed"):
        return None  # 旧记录也可能只是探测片段没有语音
    try:
        age = (date.today() - date.fromisoformat(entry.get("checked_at", ""))).days
    except (TypeError, ValueError):
        return None
    return entry if 0 <= age < CAPABILITY_CACHE_DAYS else None


@dataclass
class WindowPlan:
    mode: str  # "words" | "segments" | "text"
    windows: list[AudioWindow]
    boundaries: list[Boundary]
    params: PlanParams | None
    next_id: int
    splits: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "windows": [w.to_dict() for w in self.windows],
            "boundaries": [b.to_dict() for b in self.boundaries],
            "params": vars(self.params) if self.params else None,
            "next_id": self.next_id,
            "splits": self.splits,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WindowPlan":
        return cls(
            mode=d["mode"],
            windows=[AudioWindow.from_dict(w) for w in d["windows"]],
            boundaries=[Boundary.from_dict(b) for b in d["boundaries"]],
            params=PlanParams(**{k: v for k, v in d["params"].items() if k != "padding"})
            if d.get("params") else None,
            next_id=int(d["next_id"]),
            splits=d.get("splits", []),
        )


def is_truncated(words: list[Word], window: AudioWindow, speech: list[Region], slack: float) -> bool:
    """VAD 显示窗口末尾仍有语音，但最后一个词明显更早结束 → 疑似被截断。"""
    ends = [min(e, window.end) for s, e in speech if s < window.end and e > window.start]
    if not ends:
        return False
    last_speech = max(ends)
    last_word = max((w.end for w in words), default=window.start)
    return last_speech - last_word > slack and last_speech - window.start > slack


def missing_word_timestamps(piece: dict[str, Any]) -> bool:
    """旧版本可能把有原文但无词时间戳的窗口标记为完成。"""
    return bool(str(piece.get("text") or "").strip()) and not piece.get("words")


def _piece_path(work: Path, window: AudioWindow) -> Path:
    return work / "pieces" / f"window_{window.index:04d}.json"


def _cached_piece(work: Path, window: AudioWindow, mode: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_piece_path(work, window).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(data, dict) or mode == "words" and missing_word_timestamps(data):
        return None
    return data


def word_cache_needs_retry(work: Path) -> bool:
    path = work / "windows.json"
    if not path.is_file():
        return False
    plan = WindowPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))
    return plan.mode == "words" and any(_cached_piece(work, w, plan.mode) is None for w in plan.windows)


class WindowedTranscriber:
    def __init__(self, asr: ASRProvider, audio: Path, work: Path, speech: list[Region], duration: float,
                 chunk_cfg: dict[str, Any], language: str | None, stats: dict[str, Any],
                 report: Report | None = None, save_stats: Callable[[], None] | None = None,
                 clip_suffix: str = ".flac"):
        self.asr = asr
        self.audio = audio
        self.work = work
        self.speech = speech
        self.duration = duration
        self.cfg = chunk_cfg
        self.language = language
        self.stats = stats
        self.report = report or (lambda f, m: None)
        self.save_stats = save_stats or (lambda: None)
        self.pieces_dir = work / "pieces"
        self.plan_path = work / "windows.json"
        self.slack = float(chunk_cfg.get("truncation_slack", 3.0))
        self.min_window = float(chunk_cfg.get("min_window", 15))
        self.clip_suffix = clip_suffix  # 上传片段的格式：.flac（无损）或 .ogg（Opus）

    # ---- 统计 ------------------------------------------------------------
    def _count(self, cost: float | None = None, failed: bool = False) -> None:
        s = self.stats
        s["asr_requests"] = s.get("asr_requests", 0) + 1
        if failed:
            s["asr_failed"] = s.get("asr_failed", 0) + 1
        elif cost is None:
            s["asr_cost_unknown"] = s.get("asr_cost_unknown", 0) + 1
        else:
            s["asr_cost"] = round(s.get("asr_cost", 0.0) + cost, 6)
        self.save_stats()

    # ---- 能力探测 --------------------------------------------------------
    def detect_mode(self) -> str:
        if self.asr.capabilities().segment_timestamps:
            return "segments"
        mode = self.asr.mode
        if mode in ("words", "text"):
            return mode
        if mode != "auto":
            return "words" if self.asr.capabilities().word_timestamps else "text"
        key = getattr(self.asr, "cache_key", None)
        if key and (entry := cached_capability(key)):
            self.asr.mode = entry["mode"]
            return self.asr.mode
        start = self.speech[0][0] if self.speech else 0.0
        end = min(self.duration, start + min(PROBE_SECONDS, self.asr.capabilities().max_window))
        self.report(0.0, "探测识别能力")
        clip = media.extract_clip(self.audio, start, end, self.work / f"probe{self.clip_suffix}")
        try:
            raw = self.asr.transcribe(clip, self.language)
        except ASRError:
            self._count(failed=True)
            raise
        finally:
            clip.unlink(missing_ok=True)
        self._count(raw.cost)
        confirmed = bool(raw.words) or self.asr.mode == "text"
        detected = "words" if raw.words or (not (raw.text.strip() or raw.segments) and self.asr.mode != "text") else "text"
        self.asr.mode = detected
        if key and confirmed:
            cache_file = capability_cache_path()
            cache = _read_capability_cache()
            cache[key] = {"mode": detected, "checked_at": date.today().isoformat(), "confirmed": True}
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            write_json(cache_file, cache)
        return detected

    # ---- 计划 ------------------------------------------------------------
    def load_or_plan(self) -> WindowPlan:
        if self.plan_path.is_file():
            plan = WindowPlan.from_dict(json.loads(self.plan_path.read_text(encoding="utf-8")))
            if self.asr.mode == "auto":
                self.asr.mode = plan.mode
            return plan
        mode = self.detect_mode()
        params = params_for(self.asr.capabilities(), self.cfg, word_mode=mode == "words")
        windows, boundaries = initial_windows(self.duration, self.speech, params)
        plan = WindowPlan(mode, windows, boundaries, params, next_id=len(windows))
        self._save_plan(plan)
        return plan

    def _save_plan(self, plan: WindowPlan) -> None:
        write_json(self.plan_path, plan.to_dict())

    def _split(self, plan: WindowPlan, i: int, reason: str, shrink: bool) -> bool:
        """拆分第 i 个窗口；`shrink` 为真时同时缩小后续窗口的目标长度并重排。"""
        if plan.params is None:
            return False
        w = plan.windows[i]
        result = split_window(w, self.speech, plan.params, self.min_window)
        if result is None:
            return False
        left, right, cut = result
        left.index, right.index = plan.next_id, plan.next_id + 1
        plan.next_id += 2
        plan.windows[i:i + 1] = [left, right]
        plan.boundaries = sorted(plan.boundaries + [cut], key=lambda b: b.time)
        plan.splits.append({"window": w.index, "reason": reason, "at": round(cut.time, 3),
                            "length": round(w.duration, 1)})
        self.stats["splits"] = self.stats.get("splits", 0) + 1

        if shrink and i + 2 < len(plan.windows):
            p = plan.params
            new_target = min(p.target, w.duration * 0.6)
            if new_target < p.target:
                tol = float(self.cfg.get("tolerance", 0.3))
                p.target, p.lo, p.hi = new_target, new_target * (1 - tol), new_target * (1 + tol)
                start = right.core_end
                left_b = next((b for b in plan.boundaries if abs(b.time - start) < 1e-6), None)
                rest, rest_b = plan_windows(self.duration, self.speech, p, start=start, left=left_b,
                                            first_id=plan.next_id)
                plan.next_id += len(rest)
                plan.windows[i + 2:] = rest
                plan.boundaries = [b for b in plan.boundaries if b.time <= start + 1e-6] + rest_b
        self._save_plan(plan)
        return True

    # ---- 识别 ------------------------------------------------------------
    def piece_path(self, window: AudioWindow) -> Path:
        return _piece_path(self.work, window)

    def _lock_language(self, language: str | None) -> None:
        if language and not self.language:
            self.language = language
            self.stats["language"] = language

    def run(self) -> WindowPlan:
        self.pieces_dir.mkdir(parents=True, exist_ok=True)
        plan = self.load_or_plan()
        self.stats["mode"] = plan.mode
        if not self.language:
            for window in plan.windows:
                piece = _cached_piece(self.work, window, plan.mode)
                if piece and piece.get("language"):
                    self._lock_language(piece["language"])
                    break
        i = 0
        while i < len(plan.windows):
            w = plan.windows[i]
            n = len(plan.windows)
            self.stats["windows"] = n
            piece_path = self.piece_path(w)
            if _cached_piece(self.work, w, plan.mode) is not None:
                i += 1
                continue
            self.report(i / n, f"语音识别 {i + 1}/{n}" if n > 1 else "语音识别")
            clip = media.extract_clip(self.audio, w.start, w.end, self.work / f"clip{self.clip_suffix}")
            try:
                raw = self.asr.transcribe(clip, self.language, w)
            except (ASRTimeout, ASRTooLarge) as exc:
                self._count(failed=True)
                reason = "timeout" if isinstance(exc, ASRTimeout) else "too_large"
                if self._split(plan, i, reason, shrink=True):
                    continue
                raise ASRError(f"窗口 {w.start:.0f}–{w.end:.0f} 秒已拆到最短仍然失败：{exc}") from exc
            except ASRError:
                self._count(failed=True)
                raise
            finally:
                clip.unlink(missing_ok=True)
            self._count(raw.cost)
            words = [word.shifted(w.start) for word in raw.words]
            text = raw.text.strip() or join_texts([s.text for s in raw.segments])
            if plan.mode == "words" and text and not words:
                if self._split(plan, i, "missing_words", shrink=False):
                    continue
                raise ASRError(f"窗口 {w.start:.1f}–{w.end:.1f} 秒返回了原文，但没有词时间戳。"
                               "请重试，或改用 ASR 纯文本模式后重新识别。")
            truncated = plan.mode == "words" and is_truncated(words, w, self.speech, self.slack)
            if truncated and self._split(plan, i, "truncated", shrink=False):
                continue
            self._lock_language(raw.language)
            data = {
                "window": w.to_dict(),
                "words": [x.to_dict() for x in words],
                "text": text,
                "language": raw.language,
                "cost": raw.cost,
                "elapsed": round(raw.elapsed, 2) if raw.elapsed is not None else None,
                "truncated": truncated,
            }
            if plan.mode == "segments":
                segments = []
                for segment in raw.segments:
                    if not (math.isfinite(segment.start) and math.isfinite(segment.end)):
                        raise ASRError("识别后端返回了无效的分段时间戳")
                    start = max(w.start, segment.start + w.start)
                    end = min(w.end, segment.end + w.start)
                    if not (w.start <= start < end <= w.end):
                        raise ASRError("识别后端返回了无效的分段时间戳")
                    segments.append(Segment(start, end, segment.text).to_dict())
                if text and not segments:
                    raise ASRError("识别后端返回了原文，但没有可用的分段时间戳")
                data["segments"] = segments
            write_json(piece_path, data)
            i += 1
        self.report(1.0, "语音识别")
        return plan


def load_pieces(work: Path, plan: WindowPlan) -> list[dict[str, Any]]:
    out = []
    for w in plan.windows:
        path = _piece_path(work, w)
        out.append(json.loads(path.read_text(encoding="utf-8")))
    return out
