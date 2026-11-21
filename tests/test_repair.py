"""边界修复：风险检测、局部重新识别后的拼接、采纳规则，以及流水线中的端到端行为。"""

import json

import pytest

from yakumi import pipeline
from yakumi.asr.fake import FakeASR
from yakumi.asr.types import RawTranscript, Word
from yakumi.chunking.repair import (Issue, Score, accept, apply_repair, detect, duplicate_count,
                                    timing_anomalies, uncovered_speech)
from yakumi.pipeline import Job, run


def ticks(lo, hi, step=0.5, dur=0.4, prefix="w"):
    """每 step 秒一个词，名字按绝对时间编号，与假后端一致。"""
    out, n = [], round(lo / step)
    while n * step + dur <= hi + 1e-9:
        out.append(Word(n * step, n * step + dur, f"{prefix}{n}"))
        n += 1
    return out


# ---- 检测 -------------------------------------------------------------------
def test_uncovered_speech_finds_hole():
    words = [w for w in ticks(0, 20) if not 10 <= w.start < 11.5]
    gaps = uncovered_speech(words, [(0, 20)], 5, 15)
    # w19 结束于 9.9、w23 开始于 11.5，各加 0.25 秒余量后，空洞为 10.15～11.25
    assert len(gaps) == 1 and gaps[0][0] == pytest.approx(10.15) and gaps[0][1] == pytest.approx(11.25)
    assert uncovered_speech(ticks(0, 20), [(0, 20)], 5, 15) == []
    # 静音里没有词是正常的
    assert uncovered_speech(words, [(0, 9.9), (11.6, 20)], 5, 15) == []


def test_duplicate_count():
    clean = [Word(9.0, 9.3, "I"), Word(9.4, 9.7, "think"), Word(9.8, 9.9, "so"), Word(10.2, 10.5, "because")]
    assert duplicate_count(clean, 10.0) == 0
    dup = clean[:3] + [Word(10.0, 10.3, "I"), Word(10.4, 10.7, "think"), Word(10.8, 10.9, "so")]
    assert duplicate_count(dup, 10.0) == 1
    # 中日韩文字按 5 个字的片段比较，常见的短词尾不会误报
    ja = [Word(9.0, 9.5, "見ている"), Word(10.1, 10.6, "書いている")]
    assert duplicate_count(ja, 10.0) == 0


def test_timing_anomalies():
    assert timing_anomalies([Word(0, 5, "long")], 0, 10) == 1
    assert timing_anomalies([Word(0, 1, "a"), Word(0.5, 1.5, "b")], 0, 10) == 1
    assert timing_anomalies(ticks(0, 10), 0, 10) == 0


def test_detect_collects_signals_and_prioritises_merge_failures():
    words = [w for w in ticks(0, 60) if not 31 <= w.start < 32.5]
    boundaries = [{"time": 30.0, "kind": "silence", "risky": False},
                  {"time": 50.0, "kind": "forced", "risky": True}]
    issues = detect(words, [(0, 60)], 60, boundaries, [], {"context": 6})
    assert [i.kinds for i in issues] == [["merge"], ["gap"]]
    assert issues[1].region == (24.0, 36.0)


# ---- 拼接与采纳 -------------------------------------------------------------
def test_apply_repair_fills_hole_exactly():
    truth = ticks(0, 60)
    words = [w for w in truth if not 31 <= w.start < 32.5]
    fixed = apply_repair(words, ticks(24, 36), (24, 36), 60)
    assert [w.text for w in fixed] == [w.text for w in truth]


def test_apply_repair_at_audio_edges():
    truth = ticks(0, 20)
    words = [w for w in truth if not 1 <= w.start < 2]
    fixed = apply_repair(words, ticks(0, 8), (0, 8), 20)
    assert [w.text for w in fixed] == [w.text for w in truth]


def test_apply_repair_rejects_unmatchable_edges():
    words = ticks(0, 60)
    other = [Word(w.start, w.end, "x" + w.text) for w in ticks(24, 36)]  # 两端完全对不上
    assert apply_repair(words, other, (24, 36), 60) is None


def test_accept_rules():
    gap = Issue(["gap"], 30, (24, 36))
    assert accept(gap, Score(1.5, 0, 0), Score(0.0, 0, 0))[0]
    assert not accept(gap, Score(1.5, 0, 0), Score(1.5, 0, 0))[0]  # 没改善
    assert not accept(gap, Score(1.5, 0, 0), Score(0.0, 1, 0))[0]  # 引入了重复
    dup = Issue(["duplicate"], 30, (24, 36))
    assert accept(dup, Score(0, 2, 0), Score(0, 0, 0))[0]
    merge = Issue(["merge"], 30, (24, 36))
    assert accept(merge, Score(0, 0, 0), Score(0, 0, 0))[0]


# ---- 端到端 -----------------------------------------------------------------
class GappyFake(FakeASR):
    """长片段（正常窗口）在 drop 范围内漏词；短片段（复核）是完整的。"""

    def __init__(self, drop, always=False, **kw):
        super().__init__(**kw)
        self.drop = drop
        self.always = always

    def transcribe(self, clip, language=None, window=None):
        raw = super().transcribe(clip, language, window)
        offset = window.start if window else 0.0
        if self.always or self.calls[-1][1] - self.calls[-1][0] > 15:
            keep = [w for w in raw.words if not any(a <= w.start + offset < b for a, b in self.drop)]
            raw = RawTranscript(text=" ".join(w.text for w in keep), words=keep, language=raw.language, cost=0.0)
        return raw


def expected(duration):
    return [w.text for w in ticks(0, duration)]


@pytest.fixture
def cfg(fake_cfg):
    fake_cfg["asr"].update(target_window=30, max_window=40)
    fake_cfg["chunking"].update(vad="none", min_window=5)
    return fake_cfg


def run_to_repair(job, cfg, fake, monkeypatch):
    monkeypatch.setattr(pipeline, "create_asr", lambda c: fake)
    from yakumi.models import STAGES

    for name in STAGES[STAGES.index("repair") + 1:]:
        monkeypatch.setitem(pipeline.STAGE_FUNCS, name, lambda *a: None)
    return run(job, cfg)


def final_words(job):
    return [w["text"] for w in json.loads(job.path("words.json").read_text())["words"]]


def test_repair_recovers_words_lost_near_boundary(cfg, make_video, monkeypatch):
    video = make_video(80)
    job = Job.create(cfg["workspace"]["dir"], video, "zh")
    plan_cut = 30.0  # target 30 秒、全程无静音 → 第一个强制切点在 30 秒附近
    fake = GappyFake(drop=[(plan_cut + 2.0, plan_cut + 4.0)], target_window=30, max_window=40)
    state = run_to_repair(job, cfg, fake, monkeypatch)
    merged = [w["text"] for w in json.loads(job.path("merged.json").read_text())["words"]]
    assert len(merged) < len(expected(80))  # 合并后仍有空洞：只有一个窗口覆盖那段
    assert final_words(job) == expected(80)  # 复核后补齐
    log = json.loads(job.path("repair.json").read_text())["repairs"]
    assert any(e["accepted"] and "gap" in e["kinds"] for e in log)
    assert state.stats["repairs_accepted"] >= 1 and state.stats["repair_requests"] >= 1


def test_repair_keeps_original_when_not_better(cfg, make_video, monkeypatch):
    video = make_video(80)
    job = Job.create(cfg["workspace"]["dir"], video, "zh")
    fake = GappyFake(drop=[(32.0, 34.0)], always=True, target_window=30, max_window=40)
    state = run_to_repair(job, cfg, fake, monkeypatch)
    merged = [w["text"] for w in json.loads(job.path("merged.json").read_text())["words"]]
    assert final_words(job) == merged  # 复核结果同样漏词 → 不替换
    log = json.loads(job.path("repair.json").read_text())["repairs"]
    assert log and not any(e["accepted"] for e in log)
    assert state.stats["repairs_accepted"] == 0


def test_clean_run_needs_no_repair(cfg, make_video, monkeypatch):
    job = Job.create(cfg["workspace"]["dir"], make_video(80), "zh")
    state = run_to_repair(job, cfg, FakeASR(target_window=30, max_window=40), monkeypatch)
    assert final_words(job) == expected(80)
    assert state.stats.get("repair_requests", 0) == 0


def test_repair_disabled(cfg, make_video, monkeypatch):
    cfg["repair"]["enabled"] = False
    job = Job.create(cfg["workspace"]["dir"], make_video(80), "zh")
    fake = GappyFake(drop=[(32.0, 34.0)], target_window=30, max_window=40)
    run_to_repair(job, cfg, fake, monkeypatch)
    assert json.loads(job.path("repair.json").read_text())["skipped"]
