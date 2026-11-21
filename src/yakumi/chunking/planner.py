"""窗口规划：根据 VAD 语音区间和后端能力，把整段音频划分成识别窗口。

纯函数，不读写文件，便于测试。

- 在 [cursor + lo, cursor + hi] 范围内找切点：优先选静音最长的（超过 2 秒视为同样安全），
  其次离目标位置最近 → silence 边界。
- 范围内没有足够长的静音 → forced 边界：切在最长的微小停顿上，没有就切在目标位置。
- **所有切点两侧都加 overlap 重叠**，交给合并步骤按时间约束 LCS 去重。VAD 可能把语音
  误判为静音（实测：耳语 ASMR 中 87% 的词落在 Silero 判定的“静音”里），所以不能假设
  silence 切点处一定没有说话。
- 所有窗口的 core 首尾相接、完整覆盖 [0, duration]；窗口长度不超过 max_window。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..asr.types import ASRCapabilities, AudioWindow, Boundary

Region = tuple[float, float]
SAFE_GAP = 2.0  # 超过该长度的静音视为同样安全


@dataclass
class PlanParams:
    target: float
    lo: float  # 切点距上一个切点的最小距离
    hi: float  # 切点距上一个切点的最大距离
    max_window: float
    min_gap: float = 0.3
    overlap: float = 1.5


def gaps_of(speech: list[Region], duration: float) -> list[Region]:
    """语音区间之间（含首尾）的静音区间。"""
    out: list[Region] = []
    cursor = 0.0
    for s, e in sorted(speech):
        if s > cursor:
            out.append((cursor, s))
        cursor = max(cursor, e)
    if duration > cursor:
        out.append((cursor, duration))
    return out


def _pick_cut(gaps: list[Region], a: float, b: float, target: float, min_gap: float,
              overlap: float) -> Boundary | None:
    best: tuple[float, float] | None = None
    best_gap: Region | None = None
    for gs, ge in gaps:
        length = ge - gs
        center = (gs + ge) / 2
        if length < min_gap or not (a <= center <= b):
            continue
        key = (-min(length, SAFE_GAP), abs(center - target))
        if best is None or key < best:
            best, best_gap = key, (gs, ge)
    if best_gap is None:
        return None
    gs, ge = best_gap
    return Boundary(time=(gs + ge) / 2, kind="silence", overlap=overlap, gap=ge - gs)


def _forced_cut(gaps: list[Region], a: float, b: float, target: float, overlap: float) -> Boundary:
    micro = [(ge - gs, (gs + ge) / 2) for gs, ge in gaps if a <= (gs + ge) / 2 <= b and ge > gs]
    if micro:
        _, t = max(micro, key=lambda m: (m[0], -abs(m[1] - target)))
    else:
        t = min(max(target, a), b)
    return Boundary(time=t, kind="forced", overlap=overlap)


def build_windows(duration: float, boundaries: list[Boundary], start: float = 0.0,
                  left: Boundary | None = None, first_id: int = 0) -> list[AudioWindow]:
    """由切点生成窗口。`start`/`left` 用于只规划后半段：从已有边界 `left` 处接着往后排。"""
    cores = [start] + [b.time for b in boundaries] + [duration]
    lefts = [left] + boundaries
    windows = []
    for i in range(len(cores) - 1):
        lb = lefts[i]
        rb = boundaries[i] if i < len(boundaries) else None
        s = cores[i] - (lb.overlap if lb else 0.0)
        e = cores[i + 1] + (rb.overlap if rb else 0.0)
        windows.append(AudioWindow(
            index=first_id + i, start=round(max(0.0, s), 3), end=round(min(duration, e), 3),
            core_start=round(cores[i], 3), core_end=round(cores[i + 1], 3),
            left=lb.kind if lb else None, right=rb.kind if rb else None,
        ))
    return windows


def plan_windows(duration: float, speech: list[Region], p: PlanParams, start: float = 0.0,
                 left: Boundary | None = None, first_id: int = 0) -> tuple[list[AudioWindow], list[Boundary]]:
    hi = min(p.hi, p.max_window - 2 * p.overlap)
    lo = min(p.lo, hi)
    if hi <= 0:
        raise ValueError("max_window 太小，放不下两侧的重叠")
    gaps = gaps_of(speech, duration)
    boundaries: list[Boundary] = []
    cursor = start
    while duration - cursor > hi + 1e-9:
        a, b = cursor + lo, cursor + hi
        target = cursor + min(max(p.target, lo), hi)
        cut = _pick_cut(gaps, a, b, target, p.min_gap, p.overlap) or _forced_cut(gaps, a, b, target, p.overlap)
        boundaries.append(cut)
        cursor = cut.time
    return build_windows(duration, boundaries, start, left, first_id), boundaries


def params_for(caps: ASRCapabilities, cfg: dict[str, Any], word_mode: bool | None = None) -> PlanParams | None:
    """根据后端能力和 [chunking] 配置得到规划参数；后端不需要切分时返回 None。"""
    word_mode = caps.word_timestamps if word_mode is None else word_mode
    common = dict(min_gap=float(cfg["min_gap"]), overlap=float(cfg["overlap"]))
    if word_mode:
        if math.isinf(caps.max_window) and math.isinf(caps.target_window):
            return None
        target = min(caps.target_window, caps.max_window)
        tol = float(cfg["tolerance"])
        return PlanParams(target=target, lo=target * (1 - tol), hi=target * (1 + tol),
                          max_window=caps.max_window, **common)
    limit = min(float(cfg["text_window"]), caps.max_window)
    return PlanParams(target=limit, lo=limit * 0.4, hi=limit, max_window=limit, **common)


def plan(duration: float, speech: list[Region], caps: ASRCapabilities, cfg: dict[str, Any],
         word_mode: bool | None = None) -> tuple[list[AudioWindow], list[Boundary]]:
    p = params_for(caps, cfg, word_mode)
    if p is None or duration <= p.hi:
        return [AudioWindow(0, 0.0, duration, 0.0, duration)], []
    return plan_windows(duration, speech, p)


def split_window(window: AudioWindow, speech: list[Region], p: PlanParams,
                 min_window: float) -> tuple[AudioWindow, AudioWindow, Boundary] | None:
    """把一个窗口（例如请求超时的窗口）在中部最好的静音处一分为二；太短时返回 None。

    返回的窗口 index 沿用原窗口的值，由调用方分配新的 ID。
    """
    length = window.core_end - window.core_start
    if length < 2 * min_window:
        return None
    a = window.core_start + max(min_window, length * 0.3)
    b = window.core_end - max(min_window, length * 0.3)
    if a > b:
        a = b = window.core_start + length / 2
    mid = window.core_start + length / 2
    gaps = gaps_of(speech, window.end)
    cut = _pick_cut(gaps, a, b, mid, p.min_gap, p.overlap) or _forced_cut(gaps, a, b, mid, p.overlap)
    ext = cut.overlap
    left = AudioWindow(window.index, window.start, round(cut.time + ext, 3), window.core_start,
                       round(cut.time, 3), window.left, cut.kind)
    right = AudioWindow(window.index, round(cut.time - ext, 3), window.end, round(cut.time, 3),
                        window.core_end, cut.kind, window.right)
    return left, right, cut
