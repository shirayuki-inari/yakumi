import time

import pytest

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
    assert "开始翻译" in html and "yakumi:theme" in html
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


def test_render_rejects_bad_input(app, sample_video):
    client = app.test_client()
    with open(sample_video, "rb") as f:
        job_id = client.post("/api/jobs", data={"video": (f, "a.mp4"), "target": "zh"}).get_json()["id"]
    wait_idle(app, client, job_id)
    assert client.post(f"/api/jobs/{job_id}/render", json={"segments": []}).status_code == 400
    assert client.post(f"/api/jobs/{job_id}/render", json={"format": "doc"}).status_code == 400
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
