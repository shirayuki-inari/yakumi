"""远程部署相关：登录、安全响应头、服务器文件浏览、视频链接下载、serve / passwd 命令。"""

import os
import sys
import time
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner
from werkzeug.security import check_password_hash

from yakumi import download
from yakumi.cli import main
from yakumi.web.app import create_app
from yakumi.web.auth import hash_password

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Origin": "http://localhost"}


def wait_idle(app, client, job_id, timeout=60):
    runner = app.extensions["yakumi_runner"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not runner.busy(job_id):
            return client.get(f"/api/jobs/{job_id}").get_json()
        time.sleep(0.2)
    raise AssertionError("任务超时")


@pytest.fixture
def secured(fake_cfg):
    fake_cfg["web"]["password_hash"] = hash_password(PASSWORD)
    return create_app(cfg=fake_cfg)


def login(client, password=PASSWORD, **kwargs):
    return client.post("/login", data={"password": password, "remember": "on", **kwargs}, headers=SAME_ORIGIN)


# ---- 登录 ---------------------------------------------------------------
def test_login_required(secured):
    client = secured.test_client()
    resp = client.get("/settings")
    assert resp.status_code == 302 and "/login?next=/settings" in resp.headers["Location"]
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/files").status_code == 401
    assert client.get("/static/style.css").status_code == 200
    page = client.get("/login")
    assert page.status_code == 200 and "登录 Yakumi" in page.get_data(as_text=True)
    assert "/settings" not in page.get_data(as_text=True).split("<main")[0]  # 未登录时不显示导航
    # 修改类请求：没有来源信息时直接拒绝，同源时提示需要登录
    assert client.post("/api/jobs").status_code == 403
    assert client.post("/api/jobs", headers=SAME_ORIGIN).status_code == 401


def test_login_logout_and_redirect(secured):
    client = secured.test_client()
    assert login(client, "wrong").status_code == 401
    resp = login(client, next="/settings")
    assert resp.status_code == 302 and resp.headers["Location"] == "/settings"
    assert client.get("/").status_code == 200 and client.get("/api/jobs").status_code == 200
    assert "退出登录" in client.get("/").get_data(as_text=True)
    assert client.post("/logout", headers=SAME_ORIGIN).status_code == 302
    assert client.get("/api/jobs").status_code == 401
    # 只跳转到本站路径
    for evil in ("//evil.example/x", "https://evil.example", "/\\evil.example"):
        resp = secured.test_client().post("/login", data={"password": PASSWORD, "next": evil}, headers=SAME_ORIGIN)
        assert resp.headers["Location"] == "/"


def test_login_rate_limit(secured):
    client = secured.test_client()
    for _ in range(5):
        assert login(client, "wrong").status_code == 401
    resp = login(client)  # 密码正确也要等
    assert resp.status_code == 429 and "分钟后再试" in resp.get_data(as_text=True)


def test_password_change_logs_everyone_out(fake_cfg):
    fake_cfg["web"]["password_hash"] = hash_password(PASSWORD)
    client = create_app(cfg=fake_cfg).test_client()
    login(client)
    cookie = client.get_cookie("yakumi_session")
    assert cookie is not None
    fake_cfg["web"]["password_hash"] = hash_password("another password")
    other = create_app(cfg=fake_cfg).test_client()
    other.set_cookie("yakumi_session", cookie.value)
    assert other.get("/api/jobs").status_code == 401


def test_env_password(fake_cfg, monkeypatch):
    monkeypatch.setenv("YAKUMI_WEB_PASSWORD", "from-env-password")
    client = create_app(cfg=fake_cfg).test_client()
    assert client.get("/api/jobs").status_code == 401
    assert login(client, "from-env-password").status_code == 302
    assert client.get("/api/jobs").status_code == 200


def test_secure_cookie_and_proxy(fake_cfg):
    fake_cfg["web"]["password_hash"] = hash_password(PASSWORD)
    plain = create_app(cfg=fake_cfg).test_client()
    assert "Secure" not in login(plain).headers["Set-Cookie"]
    https = create_app(cfg=fake_cfg).test_client()
    resp = https.post("/login", data={"password": PASSWORD}, base_url="https://localhost",
                      headers={"Origin": "https://localhost"})
    assert "Secure" in resp.headers["Set-Cookie"] and "HttpOnly" in resp.headers["Set-Cookie"]
    # 反向代理后面：按 X-Forwarded-* 判断协议和域名
    fake_cfg["web"].update(proxy_count=1, allowed_hosts=["yakumi.example.com"])
    proxied = create_app(cfg=fake_cfg).test_client()
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "yakumi.example.com",
               "X-Forwarded-For": "203.0.113.5", "Origin": "https://yakumi.example.com"}
    resp = proxied.post("/login", data={"password": PASSWORD}, headers=headers)
    assert resp.status_code == 302 and "Secure" in resp.headers["Set-Cookie"]


def test_security_headers(fake_cfg):
    client = create_app(cfg=fake_cfg).test_client()
    resp = client.get("/")
    assert "script-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "DENY" and resp.headers["X-Content-Type-Options"] == "nosniff"
    # 没有设置密码时，本机使用照旧不需要登录
    assert client.get("/api/jobs").status_code == 200
    assert "退出登录" not in resp.get_data(as_text=True)


# ---- 服务器文件 ----------------------------------------------------------
@pytest.fixture
def library(fake_cfg, tmp_path, sample_video):
    workspace = Path(fake_cfg["workspace"]["dir"])
    (workspace / "videos").mkdir(parents=True)
    (workspace / "videos" / "clip.mp4").write_bytes(sample_video.read_bytes())
    (workspace / "videos" / "notes.txt").write_text("x")
    (workspace / "videos" / "talk.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello.\n", encoding="utf-8")
    extra = tmp_path / "media"
    extra.mkdir()
    (extra / "other.mkv").write_bytes(b"x")
    (tmp_path / "secret.mp4").write_bytes(b"x")
    (workspace / "videos" / "escape.mp4").symlink_to(tmp_path / "secret.mp4")
    fake_cfg["web"]["file_roots"] = [str(extra), str(tmp_path / "missing")]
    return create_app(cfg=fake_cfg), workspace, extra


def test_browse_roots_and_filters(library):
    app, workspace, extra = library
    client = app.test_client()
    data = client.get("/api/files").get_json()
    assert [r["label"] for r in data["roots"]] == ["任务目录", "media"]  # 不存在的目录不显示
    names = [e["name"] for e in data["entries"]]
    assert "videos" in names and "jobs" not in names and "uploads" not in names
    videos = client.get("/api/files?path=videos&kind=video").get_json()
    assert [e["name"] for e in videos["entries"]] == ["clip.mp4"]  # 指向外面的符号链接不显示
    assert videos["parent"] == ""
    subs = client.get("/api/files?path=videos&kind=subtitles").get_json()
    assert [e["name"] for e in subs["entries"]] == ["talk.srt"]
    assert client.get("/api/files?root=1").get_json()["entries"][0]["name"] == "other.mkv"


@pytest.mark.parametrize("query", ["path=..", "path=videos/../..", "path=/etc", "path=jobs", "path=uploads",
                                   "path=videos/escape.mp4", "root=9", "root=x", "path=nope"])
def test_browse_rejects_outside_paths(library, query):
    app, _, _ = library
    assert app.test_client().get(f"/api/files?{query}").status_code == 400


def test_create_job_from_server_file(library):
    app, workspace, _ = library
    client = app.test_client()
    resp = client.post("/api/jobs", data={"video_ref": "0:videos/clip.mp4", "target": "zh"})
    assert resp.status_code == 201, resp.get_json()
    state = wait_idle(app, client, resp.get_json()["id"])
    assert state["status"] == "done" and state["name"] == "clip.mp4"
    assert state["video"] == str((workspace / "videos" / "clip.mp4").resolve())  # 原地使用，不复制
    assert client.delete(f"/api/jobs/{state['id']}").status_code == 200
    assert (workspace / "videos" / "clip.mp4").is_file()  # 删除任务不删除服务器上的文件

    resp = client.post("/api/jobs", data={"mode": "subtitles", "subtitles_ref": "0:videos/talk.srt",
                                          "video_ref": "0:videos/clip.mp4", "target": ""})
    state = wait_idle(app, client, resp.get_json()["id"])
    assert state["status"] == "done" and state["has_video"] and state["name"] == "clip.mp4"


@pytest.mark.parametrize("ref, message", [
    ("0:videos/escape.mp4", "不允许"), ("0:../secret.mp4", "路径"), ("0:videos/notes.txt", "文件类型"),
    ("0:videos", "不是文件"), ("nope", "参数"), ("5:a.mp4", "找不到"),
])
def test_server_file_validation(library, ref, message):
    app, _, _ = library
    resp = app.test_client().post("/api/jobs", data={"video_ref": ref, "target": "zh"})
    assert resp.status_code == 400 and message in resp.get_json()["error"]


# ---- 视频链接 ------------------------------------------------------------
FAKE_YTDLP = '''
import os, shutil, sys
args = sys.argv[1:]
template = args[args.index("-o") + 1]
with open(os.environ["FAKE_YTDLP_LOG"], "a") as log:
    log.write(args[-1] + "\\n")
if os.environ.get("FAKE_YTDLP_FAIL"):
    print("ERROR: [generic] Unsupported URL: " + args[-1])
    sys.exit(1)
dest = template.replace("%(ext)s", "mp4")
print("YAKUMI-TITLE 测试视频")
print("YAKUMI-PROGRESS 50 100")
print("YAKUMI-PROGRESS 100 NA")
shutil.copyfile(os.environ["FAKE_YTDLP_VIDEO"], dest)
print("YAKUMI-FILE " + dest)
'''


@pytest.fixture
def fake_ytdlp(tmp_path, monkeypatch, sample_video):
    script = tmp_path / "fake_ytdlp.py"
    script.write_text(FAKE_YTDLP, encoding="utf-8")
    log = tmp_path / "ytdlp.log"
    monkeypatch.setenv("FAKE_YTDLP_VIDEO", str(sample_video))
    monkeypatch.setenv("FAKE_YTDLP_LOG", str(log))
    monkeypatch.setattr(download, "ytdlp_command", lambda cfg: [sys.executable, str(script)])
    return log


def test_url_job_end_to_end(fake_cfg, fake_ytdlp):
    app = create_app(cfg=fake_cfg)
    client = app.test_client()
    assert "视频链接" in client.get("/").get_data(as_text=True)
    resp = client.post("/api/jobs", data={"mode": "url", "url": "https://8.8.8.8/watch?v=1", "target": "zh",
                                          "burn": "true"})
    assert resp.status_code == 201, resp.get_json()
    job_id = resp.get_json()["id"]
    state = wait_idle(app, client, job_id)
    assert state["status"] == "done", state
    assert state["has_video"] and state["name"] == "测试视频.mp4" and state["files"]["video"]
    assert [s["key"] for s in state["steps"]][:2] == ["download", "extract"]
    assert Path(state["video"]).parent.name == job_id
    # 从头重新处理时不再下载
    client.post(f"/api/jobs/{job_id}/restart", json={})
    assert wait_idle(app, client, job_id)["status"] == "done"
    assert fake_ytdlp.read_text().splitlines() == ["https://8.8.8.8/watch?v=1"]


def test_url_job_failure(fake_cfg, fake_ytdlp, monkeypatch):
    monkeypatch.setenv("FAKE_YTDLP_FAIL", "1")
    app = create_app(cfg=fake_cfg)
    client = app.test_client()
    job_id = client.post("/api/jobs", data={"mode": "url", "url": "https://8.8.8.8/x", "burn": "true"}).get_json()["id"]
    state = wait_idle(app, client, job_id)
    assert state["status"] == "error" and "Unsupported URL" in state["error"]
    monkeypatch.delenv("FAKE_YTDLP_FAIL")
    resp = client.post(f"/api/jobs/{job_id}/render", json={"format": "srt", "burn": True, "revision": state["revision"]})
    assert resp.status_code == 200
    assert wait_idle(app, client, job_id)["files"]["video"]


def test_invalid_download_port_has_domain_error():
    with pytest.raises(download.DownloadError, match="视频链接"):
        download.check_url("https://example.invalid:invalid/video")


@pytest.mark.parametrize(("program", "message"), [
    ("import time; time.sleep(30)", "超过时限"),
    ("from pathlib import Path; Path('download.mp4').write_bytes(b'x' * 1048577)", "超过大小上限"),
])
def test_download_enforces_limits_without_progress(tmp_path, monkeypatch, program, message):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(download, "ytdlp_command", lambda _: [sys.executable, "-c", program])
    cfg = {"allow_private": True, "max_size_mb": 1, "max_duration_seconds": 1}
    started = time.monotonic()
    with pytest.raises(download.DownloadError, match=message):
        download.download("https://example.invalid/video", tmp_path, cfg)
    assert time.monotonic() - started < 5


def test_download_rejects_reported_output_outside_job(tmp_path, monkeypatch):
    output = tmp_path / "download.mp4"
    output.touch()
    job = tmp_path / "job"
    job.mkdir()
    program = f"print({('YAKUMI-FILE ' + str(output))!r})"
    monkeypatch.setattr(download, "ytdlp_command", lambda _: [sys.executable, "-c", program])
    with pytest.raises(download.DownloadError, match="不在当前任务"):
        download.download("https://example.invalid/video", job, {"allow_private": True})


@pytest.mark.parametrize("url, message", [
    ("ftp://example.com/a", "http"), ("not a url", "http"), ("http://127.0.0.1:8765/", "局域网"),
    ("http://10.0.0.8/v", "局域网"), ("http://[::1]/v", "局域网"), ("http://localhost/v", "局域网"),
])
def test_url_validation(fake_cfg, fake_ytdlp, url, message):
    resp = create_app(cfg=fake_cfg).test_client().post("/api/jobs", data={"mode": "url", "url": url})
    assert resp.status_code == 400 and message in resp.get_json()["error"]


def test_url_requires_ytdlp(fake_cfg, monkeypatch):
    monkeypatch.setattr(download, "ytdlp_command", lambda cfg: None)
    client = create_app(cfg=fake_cfg).test_client()
    assert "没有安装 yt-dlp" in client.get("/").get_data(as_text=True)
    resp = client.post("/api/jobs", data={"mode": "url", "url": "https://8.8.8.8/x"})
    assert resp.status_code == 400 and "yt-dlp" in resp.get_json()["error"]


def test_ytdlp_command_detection(monkeypatch, tmp_path):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert download.ytdlp_command({}) is None
    exe = tmp_path / "yt-dlp"
    exe.write_text("")
    assert download.ytdlp_command({"ytdlp": str(exe)}) == [str(exe)]
    assert download.ytdlp_command({"ytdlp": str(tmp_path / "missing")}) is None


def test_download_progress_parsing():
    assert download._parse_progress("YAKUMI-PROGRESS 524288 1048576") == (0.5, "下载视频 0.5/1.0 MB")
    assert download._parse_progress("YAKUMI-PROGRESS 1048576 NA")[0] == 0.0


# ---- 命令行 --------------------------------------------------------------
@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "yakumi.toml"
    path.write_text(f'# 我的配置\n[workspace]\ndir = "{tmp_path / "ws"}"\n', encoding="utf-8")
    return path


def test_serve_requires_password_when_exposed(cfg_file, monkeypatch):
    pytest.importorskip("waitress")  # 可选依赖 server；CI 会安装
    started = []
    monkeypatch.setattr("waitress.serve", lambda app, **kw: started.append(kw))
    runner = CliRunner()
    res = runner.invoke(main, ["-c", str(cfg_file), "serve", "--host", "0.0.0.0"])
    assert res.exit_code != 0 and "yakumi passwd" in res.output and not started
    res = runner.invoke(main, ["-c", str(cfg_file), "serve", "--host", "0.0.0.0", "--no-auth"])
    assert res.exit_code == 0, res.output
    assert started and started[0]["host"] == "0.0.0.0" and started[0]["max_request_body_size"] == 4096 << 20
    # 本机使用不需要密码
    res = runner.invoke(main, ["-c", str(cfg_file), "serve"])
    assert res.exit_code == 0 and len(started) == 2


def test_passwd_command(cfg_file, monkeypatch):
    pytest.importorskip("waitress")  # 可选依赖 server；CI 会安装
    started = []
    monkeypatch.setattr("waitress.serve", lambda app, **kw: started.append(kw))
    runner = CliRunner()
    res = runner.invoke(main, ["-c", str(cfg_file), "passwd"], input="short\nshort\n")
    assert res.exit_code != 0 and "8 个字符" in res.output
    res = runner.invoke(main, ["-c", str(cfg_file), "passwd"], input=f"{PASSWORD}\n{PASSWORD}\n")
    assert res.exit_code == 0, res.output
    text = cfg_file.read_text(encoding="utf-8")
    assert "# 我的配置" in text and PASSWORD not in text
    assert check_password_hash(tomllib.loads(text)["web"]["password_hash"], PASSWORD)
    res = runner.invoke(main, ["-c", str(cfg_file), "serve", "--host", "0.0.0.0"])
    assert res.exit_code == 0 and "需要登录" in res.output
    res = runner.invoke(main, ["-c", str(cfg_file), "passwd", "--clear"])
    assert res.exit_code == 0 and "password_hash" not in cfg_file.read_text(encoding="utf-8")


def test_settings_page_hides_password_hash(cfg_file):
    from yakumi import settings

    settings.write_values(cfg_file, {"web.password_hash": hash_password(PASSWORD)})
    data = settings.describe(cfg_file)
    item = next(r for r in data["readonly"] if r["id"] == "web.password_hash")
    assert item["value"].startswith("已设置") and "scrypt" not in str(data) and "pbkdf2" not in str(data)


def test_secret_file_is_private():
    from yakumi.web.auth import load_secret, secret_path

    first = load_secret()
    assert load_secret() == first and len(first) >= 32
    if os.name != "nt":
        assert secret_path().stat().st_mode & 0o777 == 0o600
