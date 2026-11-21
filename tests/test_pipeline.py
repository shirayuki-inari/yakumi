import json

import pytest

from yakumi import media, pipeline
from yakumi.models import Segment
from yakumi.pipeline import Job, run


def test_full_run_with_burn(fake_cfg, sample_video):
    job = Job.create(fake_cfg["workspace"]["dir"], sample_video, "zh", bilingual=True, burn=True)
    seen = []
    state = run(job, fake_cfg, progress=lambda s: seen.append(s.progress))
    assert state.status == "done" and state.progress == 1.0
    assert seen == sorted(seen)
    srt = job.subtitle_path.read_text(encoding="utf-8")
    assert "\n[zh] w0 w1 w2" in srt
    assert "\nw0 w1 w2" in srt  # 双语：原文在第二行
    assert job.path("preview.vtt").read_text().startswith("WEBVTT")
    assert abs(media.probe_duration(job.path("output.mp4")) - 5) < 0.5
    assert state.completed == ["extract", "vad", "transcribe", "merge", "repair", "segment", "translate", "write",
                               "burn"]
    assert state.stats["mode"] == "words" and state.stats["asr_requests"] == 1


def test_resume_skips_done_stages(fake_cfg, sample_video, monkeypatch):
    job = Job.create(fake_cfg["workspace"]["dir"], sample_video, "zh")
    run(job, fake_cfg)
    calls = []
    for name in pipeline.STAGE_FUNCS:
        orig = pipeline.STAGE_FUNCS[name]
        monkeypatch.setitem(pipeline.STAGE_FUNCS, name, lambda j, c, r, n=name, o=orig: (calls.append(n), o(j, c, r)))
    run(Job.load(fake_cfg["workspace"]["dir"], job.state.id), fake_cfg)
    assert calls == []
    run(job, fake_cfg, from_stage="write")
    assert calls == ["write"]


def test_error_is_recorded(fake_cfg, sample_video, monkeypatch):
    job = Job.create(fake_cfg["workspace"]["dir"], sample_video, "zh")

    def boom(*a):
        raise RuntimeError("坏了")

    monkeypatch.setitem(pipeline.STAGE_FUNCS, "translate", boom)
    with pytest.raises(RuntimeError):
        run(job, fake_cfg)
    saved = json.loads(job.path("state.json").read_text())
    assert saved["status"] == "error" and saved["error"] == "坏了"
    assert "translate" not in saved["completed"]


def test_edit_segments_then_rerender(fake_cfg, sample_video):
    job = Job.create(fake_cfg["workspace"]["dir"], sample_video, "zh")
    run(job, fake_cfg)
    segs = job.segments()
    segs[0] = Segment(segs[0].start, segs[0].end, segs[0].text, "改过的译文")
    job.save_segments(segs)
    assert "write" not in job.state.completed
    run(job, fake_cfg)
    assert "改过的译文" in job.subtitle_path.read_text(encoding="utf-8")


def test_transcribe_only_without_target(fake_cfg, sample_video):
    job = Job.create(fake_cfg["workspace"]["dir"], sample_video, None)
    state = run(job, fake_cfg)
    assert "translate" not in state.completed
    assert "w0 w1 w2" in job.subtitle_path.read_text(encoding="utf-8")
