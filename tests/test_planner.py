import math

import pytest

from yakumi.asr.types import ASRCapabilities
from yakumi.chunking.planner import PlanParams, gaps_of, plan, plan_windows, split_window
from yakumi.config import DEFAULTS

CHUNK = DEFAULTS["chunking"]
WORD_CAPS = ASRCapabilities(word_timestamps=True, max_window=480, target_window=300, timeout=60)


def talk(duration, every=10.0, pause=0.6, offset=0.0):
    """每 `every` 秒说一句，句间停顿 `pause` 秒。"""
    regions, t = [], offset
    while t < duration:
        regions.append((t, min(duration, t + every - pause)))
        t += every
    return regions


def check_cover(windows, duration, max_window):
    assert windows[0].core_start == 0 and windows[-1].core_end == pytest.approx(duration)
    for a, b in zip(windows, windows[1:]):
        assert a.core_end == b.core_start
        assert a.right == b.left
    for w in windows:
        assert w.start <= w.core_start < w.core_end <= w.end
        assert w.duration <= max_window + 1e-6


def in_silence(t, speech):
    return all(not (s < t < e) for s, e in speech)


def test_short_audio_single_window():
    windows, bounds = plan(200, talk(200), WORD_CAPS, CHUNK)
    assert len(windows) == 1 and bounds == []
    assert (windows[0].start, windows[0].end) == (0, 200)


def test_cuts_fall_in_silence_and_always_overlap():
    duration = 3600
    speech = talk(duration)
    windows, bounds = plan(duration, speech, WORD_CAPS, CHUNK)
    check_cover(windows, duration, 480)
    assert all(b.kind == "silence" for b in bounds)
    assert all(in_silence(b.time, speech) for b in bounds)
    # 停顿处切分也两侧各重叠 1.5 秒：VAD 判定的静音里可能有耳语
    for a, b in zip(windows, windows[1:]):
        assert a.end - a.core_end == pytest.approx(1.5)
        assert b.core_start - b.start == pytest.approx(1.5)
    lengths = [w.core_end - w.core_start for w in windows[:-1]]
    assert all(210 - 1e-6 <= n <= 390 + 1e-6 for n in lengths)


def test_prefers_longer_gap():
    duration = 1000
    speech = talk(duration)  # 0.6 秒的短停顿
    # 把 350 秒开始的那句推迟到 353 秒，制造一段 349.4～353 的 3.6 秒停顿
    speech = sorted([r for r in speech if r[0] != 350] + [(353, 359.4)])
    _, bounds = plan(duration, speech, WORD_CAPS, CHUNK)
    assert bounds[0].kind == "silence" and bounds[0].time == pytest.approx(351.2)
    assert bounds[0].gap == pytest.approx(3.6)


def test_forced_cut_with_overlap_when_no_silence():
    duration = 1000
    windows, bounds = plan(duration, [(0, duration)], WORD_CAPS, CHUNK)
    check_cover(windows, duration, 480)
    assert bounds and all(b.kind == "forced" for b in bounds)
    w0, w1 = windows[0], windows[1]
    assert w0.end - w0.core_end == pytest.approx(1.5)
    assert w1.core_start - w1.start == pytest.approx(1.5)


def test_forced_cut_prefers_micro_pause():
    duration = 1000
    speech = [(0, 320.0), (320.15, duration)]  # 只有一个 0.15 秒的停顿（小于 min_gap）
    _, bounds = plan(duration, speech, WORD_CAPS, CHUNK)
    assert bounds[0].kind == "forced" and bounds[0].time == pytest.approx(320.075)


def test_text_mode_groups_short_windows():
    duration = 300
    speech = talk(duration, every=4.0, pause=0.5)
    windows, bounds = plan(duration, speech, WORD_CAPS, CHUNK, word_mode=False)
    check_cover(windows, duration, CHUNK["text_window"])
    assert all(w.duration <= 25 for w in windows)
    assert all(b.kind == "silence" for b in bounds)


def test_unlimited_backend_not_split():
    caps = ASRCapabilities(word_timestamps=False, max_window=math.inf, target_window=math.inf)
    windows, _ = plan(7200, talk(7200), caps, CHUNK, word_mode=True)
    assert len(windows) == 1


def test_split_window_for_timeouts():
    duration = 1000
    speech = talk(duration)
    p = PlanParams(target=300, lo=210, hi=390, max_window=480)
    windows, _ = plan_windows(duration, speech, p)
    w = windows[1]
    left, right, cut = split_window(w, speech, p, min_window=15)
    assert left.core_start == w.core_start and right.core_end == w.core_end
    assert left.core_end == right.core_start == pytest.approx(cut.time)
    assert cut.kind == "silence" and in_silence(cut.time, speech)
    assert left.start == w.start and right.end == w.end
    assert split_window(type(w)(0, 0, 20, 0, 20), speech, p, min_window=15) is None


def test_gaps_of_includes_edges():
    assert gaps_of([(1, 2), (3, 4)], 5) == [(0, 1), (2, 3), (4, 5)]
