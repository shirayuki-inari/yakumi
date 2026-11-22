import time
from pathlib import Path
from threading import Event

import pytest

from yakumi.models import PIPELINE_VERSION, Segment, STAGES
from yakumi.pipeline import Job
from yakumi.web.app import create_app


def wait_idle(app, client, job_id, timeout=60):
    runner = app.extensions["yakumi_runner"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not runner.busy(job_id):
            return client.get(f"/api/jobs/{job_id}").get_json()
        time.sleep(0.2)
    raise AssertionError("任务超时")


@pytest.fixture
def app(fake_cfg):
    return create_app(cfg=fake_cfg)


def test_pages_render(app):
    client = app.test_client()
    html = client.get("/").get_data(as_text=True)
    assert "开始翻译" in html and "theme-init.js" in html
    assert "<script>" not in html  # CSP 不允许内联脚本
    assert "<svg" in html  # 图标宏可用
    assert client.get("/static/style.css").status_code == 200


def test_upload_process_edit_download(app, sample_video):
    client = app.test_client()
    with open(sample_video, "rb") as f:
        resp = client.post("/api/jobs", data={"video": (f, "我的视频.mp4"), "target": "zh",
                                              "bilingual": "true", "burn": "false"})
    assert resp.status_code == 201, resp.get_json()
    job_id = resp.get_json()["id"]

    state = wait_idle(app, client, job_id)
    assert state["status"] == "done", state
    assert state["name"] == "我的视频.mp4"
    assert state["files"]["subtitles"] and state["files"]["video"] is None
    assert state["summary"].startswith("识别：词级时间戳")

    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "我的视频.mp4" in page and "字幕校对" in page

    segs = client.get(f"/api/jobs/{job_id}/segments").get_json()
    assert segs and segs[0]["translation"].startswith("[zh]")

    edited = [{"text": s["text"], "translation": s["translation"]} for s in segs]
    edited[0]["translation"] = "手动修改"
    resp = client.post(f"/api/jobs/{job_id}/render",
                       json={"segments": edited, "format": "ass", "bilingual": False, "burn": True})
    assert resp.status_code == 200, resp.get_json()
    state = wait_idle(app, client, job_id)
    assert state["status"] == "done" and state["format"] == "ass"
    assert state["files"]["video"]

    sub = client.get(state["files"]["subtitles"])
    assert sub.status_code == 200
    assert "我的视频.zh.ass" in sub.headers["Content-Disposition"] or "filename*" in sub.headers["Content-Disposition"]
    assert "手动修改" in sub.get_data(as_text=True)
    assert client.get(state["files"]["video"]).status_code == 200

    vtt = client.get(state["files"]["preview"])
    assert vtt.mimetype == "text/vtt" and "手动修改" in vtt.get_data(as_text=True)
    src = client.get(f"/jobs/{job_id}/files/source", headers={"Range": "bytes=0-99"})
    assert src.status_code == 206
    assert src.mimetype == "video/mp4"
    assert src.headers["X-Content-Type-Options"] == "nosniff"
    assert src.headers["Content-Disposition"].startswith("inline")


@pytest.mark.parametrize("suffix", [".txt", ".mp4"])
def test_source_of_unverified_type_is_attachment(app, tmp_path, suffix):
    source = tmp_path / ("source" + suffix)
    source.write_bytes(b"unrecognized media")
    workspace = app.extensions["yakumi_runner"].cfg["workspace"]["dir"]
    job = Job.create(workspace, source, "zh")
    resp = app.test_client().get(f"/jobs/{job.state.id}/files/source")
    assert resp.get_data() == b"unrecognized media"
    assert resp.mimetype == "application/octet-stream"
    assert resp.headers["Content-Disposition"].startswith("attachment")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in resp.headers["Content-Security-Policy"]


def test_render_rejects_bad_input(app, sample_video):
    client = app.test_client()
    with open(sample_video, "rb") as f:
        job_id = client.post("/api/jobs", data={"video": (f, "a.mp4"), "target": "zh"}).get_json()["id"]
    wait_idle(app, client, job_id)
    assert client.post(f"/api/jobs/{job_id}/render", json={"segments": []}).status_code == 400
    original_state = client.get(f"/api/jobs/{job_id}").get_json()
    original = client.get(f"/api/jobs/{job_id}/segments").get_json()
    changes = [{"text": "需要保存的修改"} for _ in original]
    assert client.post(f"/api/jobs/{job_id}/render", json={"format": "doc", "segments": changes}).status_code == 400
    assert client.post(f"/api/jobs/{job_id}/render", json=["invalid"]).status_code == 400
    assert client.get(f"/api/jobs/{job_id}/segments").get_json() == original
    assert client.get(f"/api/jobs/{job_id}").get_json() == original_state
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get(f"/jobs/{job_id}/files/state.json").status_code == 404


def test_upload_validation(app):
    client = app.test_client()
    assert client.post("/api/jobs", data={}).status_code == 400


def test_glossary_api_and_page(app):
    client = app.test_client()
    assert "术语表" in client.get("/glossaries").get_data(as_text=True)
    resp = client.post("/api/glossaries", json={"name": "主播", "notes": "VTuber",
                                                "terms": [{"source": "こより", "target": "可萝莉"},
                                                          {"source": " ", "target": "空的会被忽略"}]})
    assert resp.status_code == 201 and resp.get_json()["terms"] == {"こより": "可萝莉"}
    assert client.post("/api/glossaries", json={"name": "主播"}).status_code == 409
    assert client.post("/api/glossaries", json={"name": "/"}).status_code == 400
    assert client.post("/api/glossaries", data="x").status_code == 400
    resp = client.put("/api/glossaries/主播", json={"name": "主播2", "notes": "", "terms": {"a": "b"}})
    assert resp.status_code == 200 and resp.get_json()["id"] == "主播2"
    assert [g["id"] for g in client.get("/api/glossaries").get_json()] == ["主播2"]
    assert client.put("/api/glossaries/nope", json={"name": "x"}).status_code == 404
    page = client.get("/").get_data(as_text=True)
    assert 'value="主播2"' in page
    assert client.delete("/api/glossaries/主播2").status_code == 200
    assert client.delete("/api/glossaries/主播2").status_code == 404


def test_upload_with_glossary_then_retranslate(app, sample_video):
    client = app.test_client()
    client.post("/api/glossaries", json={"name": "g", "terms": {"w0": "零"}})
    with open(sample_video, "rb") as f:
        bad = client.post("/api/jobs", data={"video": (f, "a.mp4"), "target": "zh", "glossaries": "missing"})
    assert bad.status_code == 400
    with open(sample_video, "rb") as f:
        resp = client.post("/api/jobs", data={"video": (f, "a.mp4"), "target": "zh", "glossaries": ["g"],
                                              "notes": "测试视频"})
    job_id = resp.get_json()["id"]
    state = wait_idle(app, client, job_id)
    assert state["status"] == "done" and state["glossaries"] == ["g"] and state["notes"] == "测试视频"
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "重新翻译" in page and "glossary-data" in page
    # 改名后任务里的引用同步更新
    client.put("/api/glossaries/g", json={"name": "g2", "terms": {"w0": "零"}})
    assert client.get(f"/api/jobs/{job_id}").get_json()["glossaries"] == ["g2"]
    resp = client.post(f"/api/jobs/{job_id}/retranslate", json={"glossaries": [], "notes": "改过"})
    assert resp.status_code == 200
    state = wait_idle(app, client, job_id)
    assert state["status"] == "done" and state["glossaries"] == [] and state["notes"] == "改过"
    assert client.post(f"/api/jobs/{job_id}/retranslate", json={"glossaries": ["missing"]}).status_code == 400


def test_rejects_cross_site_and_unknown_host(app):
    client = app.test_client()
    # 其他网页发来的上传（浏览器会带上 Origin）
    resp = client.post("/api/jobs", data={}, headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403
    # 同源请求照常处理（这里因为没有文件返回 400）
    assert client.post("/api/jobs", data={}, headers={"Origin": "http://localhost"}).status_code == 400
    # DNS 重绑定：用陌生域名访问
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "192.168.1.5:8765"}).status_code == 200


def test_allowed_hosts_and_upload_limit(fake_cfg):
    fake_cfg["web"] = {"allowed_hosts": ["yakumi.lan"], "max_upload_mb": 1}
    client = create_app(cfg=fake_cfg).test_client()
    assert client.get("/", headers={"Host": "yakumi.lan:8765"}).status_code == 200
    import io

    resp = client.post("/api/jobs", data={"video": (io.BytesIO(b"x" * (2 * 1024 * 1024)), "big.mp4")})
    assert resp.status_code == 413 and "1 MB" in resp.get_json()["error"]


def test_busy_job_rejects_changes(app, sample_video):
    client = app.test_client()
    with open(sample_video, "rb") as f:
        job_id = client.post("/api/jobs", data={"video": (f, "a.mp4"), "target": "zh"}).get_json()["id"]
    runner = app.extensions["yakumi_runner"]
    wait_idle(app, client, job_id)
    with runner.lock:
        runner.active.add(job_id)  # 模拟正在处理
    try:
        assert client.post(f"/api/jobs/{job_id}/render", json={}).status_code == 409
        assert client.post(f"/api/jobs/{job_id}/retranslate", json={}).status_code == 409
    finally:
        with runner.lock:
            runner.active.discard(job_id)
    assert client.post(f"/api/jobs/{job_id}/render", json={"segments": [{"text": None}]}).status_code == 400


def test_parallel_render_and_stale_revision_are_rejected(fake_cfg, tmp_path, monkeypatch):
    workspace = fake_cfg["workspace"]["dir"]
    job = Job.create(workspace, tmp_path / "source.mp4", "zh")
    job.save_segments([Segment(0, 1, "original")])
    job.state.completed = STAGES[:-1]
    job.state.status = "done"
    job.save()
    revision = job.state.revision
    first_app = create_app(cfg=fake_cfg)
    second_app = create_app(cfg=fake_cfg)
    started, release = Event(), Event()
    runs = []

    def run(job, cfg):
        runs.append(job.segments()[0].text)
        started.set()
        assert release.wait(5)
        job.state.status = "done"
        job.save()

    monkeypatch.setattr("yakumi.web.app.run", run)
    url = f"/api/jobs/{job.state.id}/render"
    first, second = first_app.test_client(), second_app.test_client()
    payload = {"segments": [{"text": "first"}], "format": "ass", "revision": revision}
    try:
        assert first.post(url, json=payload).status_code == 200
        assert started.wait(5)
        payload.update(segments=[{"text": "second"}], format="vtt")
        assert first.post(url, json=payload).status_code == 409
        assert second.post(url, json=payload).status_code == 409
    finally:
        release.set()
    assert wait_idle(first_app, first, job.state.id)["status"] == "done"
    assert second.post(url, json=payload).status_code == 409
    saved = Job.load(workspace, job.state.id)
    assert saved.state.format == "ass" and saved.segments()[0].text == "first"
    assert runs == ["first"]


def test_legacy_job_edit_is_rejected_without_migrating(fake_cfg, tmp_path, monkeypatch):
    workspace = fake_cfg["workspace"]["dir"]
    job = Job.create(workspace, tmp_path / "source.mp4", "zh")
    job.save_segments([Segment(0, 1, "old", "旧")])
    job.state.completed, job.state.status = list(STAGES[:-1]), "done"
    job.state.pipeline_version = PIPELINE_VERSION - 1
    job.save()
    before = (tmp_path / "before.json")
    before.write_bytes(job.path("state.json").read_bytes())
    app = create_app(cfg=fake_cfg)
    client = app.test_client()
    url = f"/api/jobs/{job.state.id}"

    # 迁移会删除旧字幕，页面不应提供它们用于校对
    assert client.get(f"{url}/segments").get_json() == []
    resp = client.post(f"{url}/render", json={"segments": [{"text": "edited"}]})
    assert resp.status_code == 400
    assert job.path("state.json").read_bytes() == before.read_bytes()
    assert job.path("translated.json").is_file()

    runs = []
    monkeypatch.setattr("yakumi.web.app.run", lambda job, cfg: runs.append(job.state.completed[:]))
    assert client.post(f"{url}/render", json={"format": "vtt"}).status_code == 200
    wait_idle(app, client, job.state.id)
    migrated = Job.load(workspace, job.state.id, migrate=False).state
    assert migrated.pipeline_version == PIPELINE_VERSION and migrated.format == "vtt"
    assert not job.path("translated.json").exists() and runs == [STAGES[:STAGES.index("segment")]]


SRT = ("1\n00:00:01,000 --> 00:00:02,000\nHello there.\n\n"
       "2\n00:00:02,500 --> 00:00:04,000\nThis continues,\n\n"
       "3\n00:00:04,000 --> 00:00:05,000\nand ends here.\n")


def test_transcribe_only_upload(app, sample_video):
    client = app.test_client()
    with open(sample_video, "rb") as f:
        resp = client.post("/api/jobs", data={"video": (f, "a.mp4"), "target": "", "bilingual": "true"})
    assert resp.status_code == 201, resp.get_json()
    state = wait_idle(app, client, resp.get_json()["id"])
    assert state["status"] == "done" and state["target_lang"] == "" and not state["bilingual"]
    assert state["lang_label"] == "仅识别" and "translate" not in [s["key"] for s in state["steps"]]
    segs = client.get(f"/api/jobs/{state['id']}/segments").get_json()
    assert segs and not segs[0]["translation"]


def test_import_subtitles_without_video(app):
    import io

    client = app.test_client()
    resp = client.post("/api/jobs", data={"mode": "subtitles", "target": "zh", "burn": "true",
                                          "subtitles": (io.BytesIO(SRT.encode()), "talk.en.srt")})
    assert resp.status_code == 201, resp.get_json()
    state = wait_idle(app, client, resp.get_json()["id"])
    assert state["status"] == "done", state
    assert state["source_type"] == "subtitles" and not state["has_video"] and not state["burn"]
    assert [s["key"] for s in state["steps"]] == ["translate", "write"]
    assert state["name"] == "talk.en.srt" and state["lang_label"] == "字幕 → 简体中文"
    segs = client.get(f"/api/jobs/{state['id']}/segments").get_json()
    assert [s["text"] for s in segs] == ["Hello there.", "This continues,", "and ends here."]
    assert segs[1]["group"] == segs[2]["group"] != segs[0]["group"]  # 按句末标点并句
    assert all(s["translation"] for s in segs)
    page = client.get(f"/jobs/{state['id']}").get_data(as_text=True)
    assert "<video" not in page and "只有字幕" in page and "从头重新处理" not in page
    assert client.post(f"/api/jobs/{state['id']}/render", json={"burn": True}).status_code == 400
    sub = client.get(state["files"]["subtitles"])
    assert "talk.en.zh.srt" in sub.headers["Content-Disposition"]


def test_import_subtitles_with_video_and_burn(app, sample_video):
    import io

    client = app.test_client()
    with open(sample_video, "rb") as f:
        resp = client.post("/api/jobs", data={"mode": "subtitles", "target": "", "burn": "true", "format": "vtt",
                                              "subtitles": (io.BytesIO(SRT.encode()), "a.srt"),
                                              "video": (f, "clip.mp4")})
    state = wait_idle(app, client, resp.get_json()["id"])
    assert state["status"] == "done", state
    assert state["has_video"] and state["files"]["video"] and state["name"] == "clip.mp4"
    assert [s["key"] for s in state["steps"]] == ["write", "burn"]
    assert "Hello there." in client.get(state["files"]["subtitles"]).get_data(as_text=True)


@pytest.mark.parametrize("data, message", [
    ({"mode": "subtitles"}, "请选择字幕文件"),
    ({"mode": "other"}, "任务类型"),
    ({"mode": "subtitles", "file": ("a.ass", b"x")}, "SRT 或 VTT"),
    ({"mode": "subtitles", "file": ("a.srt", "﻿".encode("utf-16"))}, "UTF-8"),
    ({"mode": "subtitles", "file": ("a.srt", b"no timing")}, "没有找到"),
    ({"mode": "subtitles", "file": ("a.srt", SRT.encode()), "target": "xx"}, "目标语言"),
])
def test_subtitle_upload_validation(app, data, message):
    import io

    data = dict(data)
    if "file" in data:
        name, raw = data.pop("file")
        data["subtitles"] = (io.BytesIO(raw), name)
    resp = app.test_client().post("/api/jobs", data=data)
    assert resp.status_code == 400 and message in resp.get_json()["error"]


def test_delete_and_restart(app, sample_video):
    client = app.test_client()
    with open(sample_video, "rb") as f:
        job_id = client.post("/api/jobs", data={"video": (f, "a.mp4"), "target": "zh"}).get_json()["id"]
    state = wait_idle(app, client, job_id)
    assert "从头重新处理" in client.get(f"/jobs/{job_id}").get_data(as_text=True)

    assert client.post(f"/api/jobs/{job_id}/restart", json={"revision": state["revision"] - 1}).status_code == 409
    resp = client.post(f"/api/jobs/{job_id}/restart", json={"revision": state["revision"]})
    assert resp.status_code == 200
    state = wait_idle(app, client, job_id)
    assert state["status"] == "done" and state["stats"]["asr_requests"] == 1  # 统计从头计算

    runner = app.extensions["yakumi_runner"]
    with runner.lock:
        runner.active.add(job_id)
    try:
        assert client.delete(f"/api/jobs/{job_id}").status_code == 409
    finally:
        with runner.lock:
            runner.active.discard(job_id)
    job_dir = Path(runner.cfg["workspace"]["dir"]) / "jobs" / job_id
    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    assert not job_dir.exists()
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.delete(f"/api/jobs/{job_id}").status_code == 404
    assert job_id not in client.get("/").get_data(as_text=True)


def test_restart_without_source_video(app, tmp_path):
    video = tmp_path / "gone.mp4"
    video.write_bytes(b"x")
    workspace = app.extensions["yakumi_runner"].cfg["workspace"]["dir"]
    job = Job.create(workspace, video, "zh")
    video.unlink()
    resp = app.test_client().post(f"/api/jobs/{job.state.id}/restart", json={})
    assert resp.status_code == 400 and "原视频" in resp.get_json()["error"]


def test_setup_banner(fake_cfg, monkeypatch):
    for env in ("OPENROUTER_API_KEY", "YAKUMI_TRANSLATE_API_KEY", "YAKUMI_ASR_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    fake_cfg["translate"].update(provider="openai", api_key="")
    page = create_app(cfg=fake_cfg).test_client().get("/").get_data(as_text=True)
    assert "还有设置没有完成" in page and "翻译 API Key" in page
