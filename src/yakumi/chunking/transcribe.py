"""分窗识别：能力探测 → 规划窗口 → 逐窗识别（缓存、超时拆分、截断检测、费用统计）。

产物（都在任务目录下）：
    windows.json          当前的窗口计划（识别过程中可能因拆分而更新）
    pieces/window_NNNN.json  每个窗口的识别结果，词为全局时间
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import media
from ..files import cache_dir, write_json
from ..asr.base import ASRError, ASRProvider, ASRTimeout, ASRTooLarge
from ..asr.types import AudioWindow, Boundary, Word
from .planner import PlanParams, params_for, plan_windows, split_window

Region = tuple[float, float]
Report = Callable[[float, str], None]
PROBE_SECONDS = 12.0


def capability_cache_path() -> Path:
    return cache_dir() / "capabilities.json"


@dataclass
class WindowPlan:
    mode: str  # "words" | "text"
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
        mode = self.asr.mode
        if mode in ("words", "text"):
            return mode
        if mode != "auto":
            return "words" if self.asr.capabilities().word_timestamps else "text"
        key = getattr(self.asr, "cache_key", None)
        cache_file = capability_cache_path()
        cache: dict[str, Any] = {}
        if key and cache_file.is_file():
            try:
                cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except ValueError:
                cache = {}
            if key in cache:
                self.asr.mode = cache[key]["mode"]
                return self.asr.mode
        start = self.speech[0][0] if self.speech else 0.0
        end = min(self.duration, start + PROBE_SECONDS)
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
        detected = "words" if raw.words else "text"
        self.asr.mode = detected
        if key:
            cache[key] = {"mode": detected, "checked_at": time.strftime("%Y-%m-%d")}
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
        if params is None or self.duration <= params.hi:
            windows = [AudioWindow(0, 0.0, self.duration, 0.0, self.duration)]
            boundaries: list[Boundary] = []
        else:
            windows, boundaries = plan_windows(self.duration, self.speech, params)
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
        return self.pieces_dir / f"window_{window.index:04d}.json"

    def run(self) -> WindowPlan:
        self.pieces_dir.mkdir(parents=True, exist_ok=True)
        plan = self.load_or_plan()
        self.stats["mode"] = plan.mode
        i = 0
        while i < len(plan.windows):
            w = plan.windows[i]
            n = len(plan.windows)
            self.stats["windows"] = n
            if self.piece_path(w).is_file():
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
            truncated = plan.mode == "words" and is_truncated(words, w, self.speech, self.slack)
            if truncated and self._split(plan, i, "truncated", shrink=False):
                continue
            if raw.language and not self.language:
                self.language = raw.language  # 锁定语言，后续窗口不再各自检测
                self.stats["language"] = raw.language
            write_json(self.piece_path(w), {
                "window": w.to_dict(),
                "words": [x.to_dict() for x in words],
                "text": raw.text,
                "language": raw.language,
                "cost": raw.cost,
                "elapsed": round(raw.elapsed, 2) if raw.elapsed is not None else None,
                "truncated": truncated,
            })
            i += 1
        self.report(1.0, "语音识别")
        return plan


def load_pieces(work: Path, plan: WindowPlan) -> list[dict[str, Any]]:
    out = []
    for w in plan.windows:
        path = work / "pieces" / f"window_{w.index:04d}.json"
        out.append(json.loads(path.read_text(encoding="utf-8")))
    return out
