"""分窗识别：多窗口合并、超时拆分、续跑、纯文本模式、能力探测与截断检测。

假后端按绝对时间每 0.5 秒生成一个词 w{n}，所以正确的合并结果恰好是 w0, w1, …, 不多不少。
"""

import json

import pytest

from yakumi import pipeline
from yakumi.asr.fake import FakeASR
from yakumi.asr.types import AudioWindow, RawTranscript, Word
from yakumi.chunking.merge import clean_words
from yakumi.chunking.transcribe import WindowedTranscriber, capability_cache_path, is_truncated
from yakumi.models import STAGES
from yakumi.pipeline import Job, run
from yakumi.segment.timing import place_sentences


def expected_words(duration):
    n = 0
    out = []
    while n * 0.5 + 0.4 <= duration + 1e-6:
        out.append(f"w{n}")
        n += 1
    return out


def run_until(job, cfg, last="repair"):
    """只跑到某个阶段为止。"""
    stop = STAGES.index(last)
    original = dict(pipeline.STAGE_FUNCS)
    try:
        for name in STAGES[stop + 1:]:
            pipeline.STAGE_FUNCS[name] = lambda *a: None
        return run(job, cfg)
    finally:
        pipeline.STAGE_FUNCS.update(original)


def words_of(job):
    data = json.loads(job.path("words.json").read_text())
    return [w["text"] for w in data["words"]]


@pytest.fixture
def cfg(fake_cfg):
    fake_cfg["asr"].update(target_window=4, max_window=8)
    fake_cfg["chunking"].update(vad="none", min_window=1, overlap=1.5)
    return fake_cfg


def test_multi_window_merge_is_exact(cfg, make_video):
    video = make_video(20)
    job = Job.create(cfg["workspace"]["dir"], video, "zh")
    state = run_until(job, cfg)
    plan = json.loads(job.path("windows.json").read_text())
    assert len(plan["windows"]) >= 4
    assert all(b["kind"] == "forced" for b in plan["boundaries"])  # 没有静音，全部强制切分
    assert words_of(job) == expected_words(20)
    assert state.stats["asr_requests"] == len(plan["windows"])
    assert state.stats["asr_cost"] == 0.0


def test_timeout_splits_and_shrinks(cfg, make_video):
    cfg["asr"].update(target_window=8, max_window=12, fake_timeout_over=5)
    video = make_video(30)
    job = Job.create(cfg["workspace"]["dir"], video, "zh")
    state = run_until(job, cfg)
    plan = json.loads(job.path("windows.json").read_text())
    assert plan["splits"] and plan["splits"][0]["reason"] == "timeout"
    assert all(w["end"] - w["start"] <= 5 + 1e-6 for w in plan["windows"])
    # 第一次超时后，后续窗口的目标长度被调小，不会每个窗口都先超时一次
    assert state.stats["asr_failed"] < len(plan["windows"])
    assert words_of(job) == expected_words(30)


def test_resume_only_redoes_missing_windows(cfg, make_video, monkeypatch):
    shared = FakeASR(target_window=4, max_window=8)
    monkeypatch.setattr(pipeline, "create_asr", lambda c: shared)
    video = make_video(20)
    job = Job.create(cfg["workspace"]["dir"], video, "zh")
    run_until(job, cfg)
    total = len(shared.calls)
    pieces = sorted(job.path("pieces").glob("window_*.json"))
    pieces[2].unlink()
    job.invalidate_from("merge")
    job.state.completed.remove("transcribe")
    job.save()
    run_until(Job.load(cfg["workspace"]["dir"], job.state.id), cfg)
    assert len(shared.calls) == total + 1
    assert words_of(job) == expected_words(20)


def test_force_clears_window_cache(cfg, make_video, monkeypatch):
    shared = FakeASR(target_window=4, max_window=8)
    monkeypatch.setattr(pipeline, "create_asr", lambda c: shared)
    job = Job.create(cfg["workspace"]["dir"], make_video(10), "zh")
    run_until(job, cfg)
    first = len(shared.calls)
    run_until(job, cfg)  # 全部已完成，不再请求
    assert len(shared.calls) == first
    job.invalidate_from("extract")
    run_until(job, cfg)
    assert len(shared.calls) == 2 * first


def test_text_mode_pipeline(cfg, make_video):
    cfg["asr"]["mode"] = "text"
    cfg["chunking"]["text_window"] = 6
    job = Job.create(cfg["workspace"]["dir"], make_video(20), "zh")
    state = run_until(job, cfg, last="segment")
    assert state.stats["mode"] == "text"
    plan = json.loads(job.path("windows.json").read_text())
    assert all(w["end"] - w["start"] <= 6 + 1e-6 for w in plan["windows"])
    segs = json.loads(job.path("aligned.json").read_text())
    text = " ".join(s["text"] for s in segs).split()
    # 重叠区的词被两个窗口各识别了一次，去重后应当不多不少
    assert text == expected_words(20)
    # 与音频的实际长度比较：不同 ffmpeg 版本生成的 AAC 会比 20 秒多出几毫秒的编码填充
    duration = json.loads(job.path("vad.json").read_text())["duration"]
    assert segs[0]["start"] >= 0 and segs[-1]["end"] <= duration + 1e-6
    data = json.loads(job.path("words.json").read_text())
    assert all(b["strategy"] == "text_overlap" for b in data["boundaries"])


def test_upload_as_opus(cfg, make_video):
    cfg["asr"]["upload_format"] = "opus"
    job = Job.create(cfg["workspace"]["dir"], make_video(10), "zh")
    run_until(job, cfg)
    assert words_of(job) == expected_words(10)


def test_audio_removed_after_done_and_reextracted(cfg, make_video):
    job = Job.create(cfg["workspace"]["dir"], make_video(10), "zh")
    run(job, cfg)
    assert not job.path("audio.wav").exists()
    run(job, cfg, from_stage="transcribe")  # 需要音频的阶段重跑时自动重新抽取
    assert job.state.status == "done" and words_of(job) == expected_words(10)
    cfg["workspace"]["keep_audio"] = True
    run(job, cfg, force=True)
    assert job.path("audio.wav").exists()


def test_merge_reports_recorded(cfg, make_video):
    job = Job.create(cfg["workspace"]["dir"], make_video(20), "zh")
    state = run_until(job, cfg)
    data = json.loads(job.path("words.json").read_text())
    assert data["boundaries"] and all(b["strategy"] == "lcs" for b in data["boundaries"])
    assert state.stats["risky_boundaries"] == 0


class AutoStub:
    """模拟 mode=auto 的后端：返回词级结果。"""

    name = "stub"
    mode = "auto"
    cache_key = "http://x|m"

    def __init__(self):
        self.calls = 0

    def capabilities(self):
        from yakumi.asr.types import ASRCapabilities

        return ASRCapabilities(word_timestamps=self.mode != "text", max_window=8, target_window=4)

    def transcribe(self, clip, language=None, window=None):
        self.calls += 1
        return RawTranscript(text="hi", words=[Word(0.1, 0.3, "hi")], cost=0.001)


def test_probe_detects_and_caches_mode(tmp_path, make_video):
    from yakumi import media

    audio = media.extract_audio(make_video(3), tmp_path / "a.flac")
    stub = AutoStub()
    stats = {}
    t = WindowedTranscriber(stub, audio, tmp_path, [(0.5, 2.5)], 3.0, {"tolerance": 0.3}, None, stats)
    assert t.detect_mode() == "words" and stub.mode == "words"
    assert stats["asr_requests"] == 1 and stats["asr_cost"] == 0.001
    cache = json.loads(capability_cache_path().read_text())
    assert cache["http://x|m"]["mode"] == "words"
    stub2 = AutoStub()
    WindowedTranscriber(stub2, audio, tmp_path, [], 3.0, {}, None, {}).detect_mode()
    assert stub2.calls == 0 and stub2.mode == "words"


def test_is_truncated():
    w = AudioWindow(0, 100, 200, 100, 200)
    speech = [(100, 199.5)]
    assert is_truncated([Word(101, 150, "a")], w, speech, slack=3)
    assert not is_truncated([Word(101, 198, "a")], w, speech, slack=3)
    assert not is_truncated([Word(101, 150, "a")], w, [(100, 151)], slack=3)  # 后面本来就是静音
    assert not is_truncated([], w, [], slack=3)


def test_clean_words_fixes_spike_anomalies():
    # 实测出现过的异常：end < start，以及时长为 0
    words = [Word(0.9, 1.4, "おい、"), Word(1.5, 1.4, "と"), Word(1.4, 1.4, "また"), Word(1.8, 2.0, "声")]
    out = clean_words(words)
    assert [w.text for w in out] == ["おい、", "と", "また", "声"]
    assert all(w.end >= w.start for w in out)
    assert out[2].end > out[2].start  # 0 时长的词被扩展


def test_place_sentences_follows_speech():
    segs = place_sentences(["First one.", "Second sentence here."], (10, 30), [(12, 14), (20, 26), (40, 50)])
    # 只铺在窗口内的语音区间上（12～14、20～26，共 8 秒），按字数比例分配
    assert segs[0].start == 12 and segs[-1].end == 26
    assert [s.text for s in segs] == ["First one.", "Second sentence here."]
