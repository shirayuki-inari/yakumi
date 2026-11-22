import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from yakumi import files, media, pipeline
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


def test_atomic_json_writers_use_independent_temporary_files(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    barrier = Barrier(2, timeout=5)
    replace = files.os.replace

    def replace_together(src, dst):
        barrier.wait()
        replace(src, dst)

    monkeypatch.setattr(files.os, "replace", replace_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda n: files.write_json(path, {"value": n}), [1, 2]))

    assert files.read_json(path) in ({"value": 1}, {"value": 2})
    assert list(tmp_path.iterdir()) == [path]


def test_job_creation_allocates_a_fresh_directory(tmp_path, monkeypatch):
    allocated = iter(("existing", "existing", "new"))
    monkeypatch.setattr(pipeline, "new_job_id", lambda: next(allocated))
    first = Job.create(tmp_path, None, "zh")
    first.path("windows.json").write_text("{}", encoding="utf-8")
    second = Job.create(tmp_path, None, "en")
    assert second.state.id == "new" and not second.path("windows.json").exists()
    assert Job.load(tmp_path, "existing").state.target_lang == "zh"
    with pytest.raises(pipeline.PipelineError, match="任务已存在"):
        Job.create(tmp_path, None, "en", job_id="existing")


def test_translation_checkpoint_survives_interrupt_before_export(fake_cfg, monkeypatch):
    workspace = fake_cfg["workspace"]["dir"]
    job = Job.create(workspace, None, "zh")
    job.import_subtitles([Segment(0, 1, "Original")])
    save = job.save

    def stop_before_export():
        if job.state.stage == "write":
            raise KeyboardInterrupt
        save()

    monkeypatch.setattr(job, "save", stop_before_export)
    with pytest.raises(KeyboardInterrupt):
        run(job, fake_cfg)
    resumed = Job.load(workspace, job.state.id)
    assert resumed.path("translated.json").is_file() and "translate" in resumed.state.completed
    monkeypatch.setitem(pipeline.STAGE_FUNCS, "translate", lambda *_: pytest.fail("重复翻译"))
    run(resumed, fake_cfg)
    assert resumed.subtitle_path.is_file()
