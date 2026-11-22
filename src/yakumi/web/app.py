"""网页工作台：上传视频或字幕、查看进度、校对字幕、重新生成与下载，以及背景资料、设置和环境检查。

任务在进程内的单线程池里排队执行，状态全部来自任务目录的 state.json，
不需要数据库或消息队列。

默认只监听本机、不需要登录；部署到远程服务器时用 yakumi passwd 设置登录密码（见 auth.py）。
为了不被其他网页利用（访问恶意网页时，浏览器可以向工作台发请求，替你上传文件、发起要花钱的识别和翻译），
所有修改类请求都会检查：
- Origin：浏览器带上的来源必须和本站一致，拒绝跨站请求；启用登录后，没有来源信息的修改请求也会被拒绝；
- Host：只接受 IP 地址、localhost 和配置里 web.allowed_hosts 列出的域名，防止 DNS 重绑定。
页面另外带有 CSP 等安全响应头。
"""

from __future__ import annotations

import copy
import ipaddress
import json
import logging
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix

from .. import download as downloader, glossary as glossary_store, media, settings as settings_store
from ..config import ConfigError, load_config
from ..doctor import local_checks, online_checks, setup_problems
from ..files import read_json, write_json
from ..models import STAGES, JobState, Segment, segments_from_list
from ..stats import format_stats, stats_report
from ..pipeline import STAGE_LABELS, Job, JobBusyError, PipelineError, enabled_stages, first_stage, run
from ..subtitle.formats import FORMATS, parse_srt
from ..terms import MODES as TERM_MODES, sentences_from_cues
from ..terms import review as term_review
from ..translate.base import LANG_NAMES, lang_name
from .auth import Auth
from .browse import Browser, FileAccessError, make_roots

LANGUAGES = [
    ("zh", "简体中文"), ("zh-TW", "繁體中文"), ("en", "English"), ("ja", "日本語"), ("ko", "한국어"),
    ("fr", "Français"), ("de", "Deutsch"), ("es", "Español"), ("pt", "Português"), ("ru", "Русский"),
    ("it", "Italiano"), ("vi", "Tiếng Việt"), ("th", "ไทย"), ("id", "Bahasa Indonesia"), ("ar", "العربية"),
]
LANG_CODES = {code for code, _ in LANGUAGES}
STATUS_LABELS = {"pending": "排队中", "running": "处理中", "awaiting_review": "待审核", "done": "已完成",
                 "error": "失败"}
MEDIA_TYPES = {"srt": "application/x-subrip", "ass": "text/x-ssa", "vtt": "text/vtt"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SUBTITLE_SUFFIXES = (".srt", ".vtt")
MAX_SUBTITLE_BYTES = 50 * 1024 * 1024
PUBLIC_ENDPOINTS = {"login", "static"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")


class _SessionInterface(SecureCookieSessionInterface):
    """通过 HTTPS 访问（含反向代理转发的 HTTPS）时，登录 Cookie 只走 HTTPS。"""

    def get_cookie_secure(self, app: Flask) -> bool:
        return request.is_secure


def safe_next(target: str | None) -> str:
    """登录后跳转的地址只能是本站的路径。"""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target

log = logging.getLogger(__name__)


def host_allowed(host: str, allowed: list[str]) -> bool:
    """Host 头是否可信：IP 地址、localhost，或配置里明确允许的域名。"""
    name = urlsplit(f"//{host}").hostname or ""
    if name == "localhost" or name in allowed:
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


class Runner:
    """串行执行任务，同一任务不会重复入队。"""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yakumi-job")
        self.active: set[str] = set()
        self.lock = threading.Lock()

    @contextmanager
    def reserve(self, job_id: str) -> Iterator[ExitStack]:
        with self.lock:
            if job_id in self.active:
                raise JobBusyError("任务正在处理中，请稍候")
            self.active.add(job_id)
        with ExitStack() as reservation:
            reservation.callback(self._release, job_id)
            try:
                reservation.enter_context(Job.lock(self.cfg["workspace"]["dir"], job_id))
            except JobBusyError:
                raise
            except PipelineError:
                abort(404)
            yield reservation

    def _release(self, job_id: str) -> None:
        with self.lock:
            self.active.discard(job_id)

    def submit(self, job: Job, reservation: ExitStack, from_stage: str | None = None) -> None:
        job.state.status, job.state.error, job.state.message = "pending", None, "排队中"
        # 网页任务按服务配置执行。清除它即将替换的 CLI 阶段指纹。
        completed = set(job.state.completed)
        if from_stage:
            completed.difference_update(STAGES[STAGES.index(from_stage):])
        if job.state.stage_inputs is not None:
            for stage in job.state.stage_inputs.keys() - completed:
                job.state.stage_inputs[stage] = ""
        if from_stage:
            job.invalidate_from(from_stage)
        else:
            job.save()
        worker_reservation = reservation.pop_all()
        try:
            self.pool.submit(self._run, job, worker_reservation)
        except Exception:
            with worker_reservation:
                job.state.status, job.state.error, job.state.message = "error", "无法提交后台任务", "处理失败"
                job.save()
            raise

    def busy(self, job_id: str) -> bool:
        with self.lock:
            return job_id in self.active

    def _run(self, job: Job, reservation: ExitStack) -> None:
        with reservation:
            try:
                run(job, self.cfg)
            except Exception as exc:
                log.exception("任务 %s 失败", job.state.id)
                if job.state.status != "error":
                    try:
                        job.state.status, job.state.error, job.state.message = "error", str(exc), "处理失败"
                        job.save()
                    except OSError:
                        log.exception("无法记录任务 %s 的错误状态", job.state.id)


def _lang_label(state: JobState) -> str:
    imported = state.source_type == "subtitles"
    if not state.target_lang:
        return "导入字幕" if imported else "仅识别"
    src = "字幕" if imported else (lang_name(state.source_lang) if state.source_lang else "自动识别")
    return f"{src} → {lang_name(state.target_lang)}"


def _steps(state: JobState) -> list[dict[str, str]]:
    """任务页的步骤条：每个阶段已完成、进行中、失败还是未开始。"""
    steps = []
    for stage in enabled_stages(state):
        if stage == state.stage and state.status == "error":
            status = "error"
        elif stage in state.completed:
            status = "done"
        elif stage == state.stage and state.status in ("running", "awaiting_review"):
            status = "active"
        else:
            status = "todo"
        steps.append({"key": stage, "label": STAGE_LABELS[stage], "status": status})
    return steps


def _view(state: JobState) -> dict[str, Any]:
    data = state.to_dict()
    data.update(
        name=Path(state.video).name if state.video else "字幕",
        lang_label=_lang_label(state),
        status_label=STATUS_LABELS.get(state.status, state.status),
        created=time.strftime("%m-%d %H:%M", time.localtime(state.created_at)),
        has_video=bool(state.video),
        steps=_steps(state),
    )
    return data


def create_app(config_path: str | None = None, cfg: dict[str, Any] | None = None) -> Flask:
    """`cfg` 由调用方直接提供时（测试、嵌入使用），网页保存的设置会写入配置文件并叠加到它上面。"""
    injected = cfg is not None
    cfg = cfg or load_config(config_path)
    workspace = Path(cfg["workspace"]["dir"]).resolve()
    cfg["workspace"]["dir"] = str(workspace)
    (workspace / "uploads").mkdir(parents=True, exist_ok=True)

    app = Flask(__name__)
    app.json.ensure_ascii = False
    web_cfg = cfg.get("web", {})
    max_mb = int(web_cfg.get("max_upload_mb", 4096))
    app.config["MAX_CONTENT_LENGTH"] = max_mb * 1024 * 1024 if max_mb > 0 else None
    allowed_hosts = [str(h).lower() for h in web_cfg.get("allowed_hosts", [])]
    proxies = int(web_cfg.get("proxy_count", 0) or 0)
    if proxies > 0:  # 反向代理后面：按代理转发的头确定客户端地址、协议和域名
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxies, x_proto=proxies, x_host=proxies)  # type: ignore[method-assign]

    auth = Auth(web_cfg)
    app.extensions["yakumi_auth"] = auth
    app.secret_key = auth.secret
    app.session_interface = _SessionInterface()
    app.config.update(SESSION_COOKIE_NAME="yakumi_session", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=int(web_cfg.get("session_days", 30) or 30)))
    browser = Browser(make_roots(workspace, list(web_cfg.get("file_roots") or [])))

    def logged_in() -> bool:
        return not auth.enabled or session.get("auth") == auth.fingerprint

    @app.context_processor
    def template_globals():
        return {"auth_enabled": auth.enabled, "logged_in": logged_in()}

    @app.before_request
    def guard():
        if not host_allowed(request.host.lower(), allowed_hosts):
            return jsonify(error="不允许的访问地址（Host）。需要用域名访问时，请把它加入配置 web.allowed_hosts"), 403
        if request.method in UNSAFE_METHODS:
            origin = request.headers.get("Origin")
            if not origin and auth.enabled and request.headers.get("Referer"):
                parts = urlsplit(request.headers["Referer"])
                origin = f"{parts.scheme}://{parts.netloc}"
            if origin and urlsplit(origin).netloc.lower() != request.host.lower():
                return jsonify(error="拒绝跨站请求"), 403
            if not origin and auth.enabled:
                return jsonify(error="拒绝没有来源信息的请求"), 403
        if request.endpoint not in PUBLIC_ENDPOINTS and not logged_in():
            if request.method == "GET" and not request.path.startswith("/api/"):
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            return jsonify(error="需要登录", login=url_for("login")), 401
        return None

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if resp.mimetype == "text/html":
            resp.headers.setdefault("Content-Security-Policy", CSP)
        return resp

    @app.route("/login", methods=["GET", "POST"])
    def login():
        target = safe_next(request.values.get("next"))
        if not auth.enabled or logged_in():
            return redirect(target)
        error, status = None, 200
        if request.method == "POST":
            client = request.remote_addr or "?"
            wait = auth.wait_seconds(client)
            if wait:
                error, status = f"尝试次数太多，请 {max(1, wait // 60)} 分钟后再试", 429
            elif auth.verify(request.form.get("password", "")):
                auth.reset(client)
                session.clear()
                session["auth"] = auth.fingerprint
                session.permanent = request.form.get("remember") == "on"
                return redirect(target)
            else:
                auth.record_failure(client)
                error, status = "密码不正确", 401
        return render_template("login.html", error=error, next=target), status

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login") if auth.enabled else url_for("index"))

    @app.errorhandler(413)
    def too_large(_exc):
        return jsonify(error=f"文件太大，上限为 {max_mb} MB（可以修改配置 web.max_upload_mb）"), 413

    @app.errorhandler(JobBusyError)
    def job_busy(exc):
        return jsonify(error=str(exc)), 409

    runner = Runner(cfg)
    app.extensions["yakumi_runner"] = runner
    settings_lock = threading.Lock()
    fixed_web = copy.deepcopy(web_cfg)

    def apply_settings(result: settings_store.SaveResult) -> None:
        """保存设置后换上新配置：之后开始处理的任务使用它，正在处理的任务不受影响。

        任务目录和 [web] 在启动时确定，修改后需要重启。
        """
        if injected:
            new = copy.deepcopy(runner.cfg)
            for fid in result.changed:
                section, key = fid.split(".", 1)
                new[section][key] = result.cfg[section][key]
        else:
            new = result.cfg
        new["workspace"]["dir"] = str(workspace)
        new["web"] = copy.deepcopy(fixed_web)
        runner.cfg = new

    # 上次进程退出时仍在运行的任务已经中断，标记出来，方便用户点“继续”。
    for state in Job.list(workspace):
        if state.status in ("running", "pending"):
            try:
                with Job.lock(workspace, state.id):
                    job = Job.load(workspace, state.id, migrate=False)
                    if job.state.status in ("running", "pending"):
                        job.state.status, job.state.error = "error", "服务重启，任务已中断，可以点击“继续处理”接着处理"
                        job.save()
            except JobBusyError:
                continue

    def load(job_id: str) -> Job:
        try:
            return Job.load(workspace, job_id, migrate=False)
        except PipelineError:
            abort(404)

    def view(job: Job) -> dict[str, Any]:
        data = _view(job.state)
        meta = job.path("meta.json")
        if meta.is_file():
            data["name"] = json.loads(meta.read_text(encoding="utf-8")).get("name") or data["name"]
        data["busy"] = runner.busy(job.state.id)
        data["summary"] = format_stats(job.state.stats)
        data["report"] = stats_report(job.state.stats)
        review = term_review.load_review(job.dir) if job.state.term_mode != "off" else None
        data["terms"] = ({**review.summary(), "finished": review.finished} if review else None)
        data["files"] = {
            "subtitles": url_for("job_download", job_id=job.state.id, kind="subtitles")
            if "subtitles" in job.state.outputs else None,
            "video": url_for("job_download", job_id=job.state.id, kind="video")
            if "video" in job.state.outputs else None,
            "preview": url_for("job_file", job_id=job.state.id, name="preview")
            if job.path("preview.vtt").is_file() else None,
        }
        return data

    def glossary_summaries() -> list[dict[str, Any]]:
        return [{"id": g.id, "name": g.name, "count": len(g.entries),
                 "summary": g.notes.strip().splitlines()[0][:80] if g.notes.strip() else ""}
                for g in glossary_store.list_all()]

    def to_json(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    def check_glossaries(ids: Any) -> list[str]:
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError("背景资料参数格式不正确")
        known = {g["id"] for g in glossary_summaries()}
        missing = [i for i in ids if i not in known]
        if missing:
            raise ValueError(f"找不到背景资料：{'、'.join(missing)}")
        return ids

    # ---- 页面 ----------------------------------------------------------
    @app.get("/")
    def index():
        jobs = [view(load(s.id)) for s in Job.list(workspace)[:50]]
        sub = runner.cfg["subtitle"]
        return render_template("index.html", jobs=jobs, languages=LANGUAGES, glossaries=glossary_summaries(),
                               default_target="zh", default_bilingual=bool(sub["bilingual"]),
                               default_terms=str(runner.cfg.get("terms", {}).get("mode", "off")),
                               default_format=sub["format"], formats=FORMATS,
                               problems=setup_problems(runner.cfg),
                               ytdlp=downloader.available(runner.cfg.get("download", {})))

    @app.get("/jobs/<job_id>")
    def job_page(job_id: str):
        job = load(job_id)
        data = view(job)
        return render_template("job.html", job=data, job_json=to_json(data),
                               glossaries_json=to_json(glossary_summaries()),
                               categories=glossary_store.CATEGORIES)

    @app.get("/settings")
    def settings_page():
        try:
            data = settings_store.describe(config_path)
        except ConfigError as exc:
            return render_template("settings.html", settings_json="null", config_error=str(exc))
        return render_template("settings.html", settings_json=to_json(data), config_error=None)

    @app.get("/context")
    def context_page():
        items = [g.to_dict() for g in glossary_store.list_all()]
        return render_template("context.html", glossaries=items, glossaries_json=to_json(items),
                               glossaries_dir=str(glossary_store.glossaries_dir()),
                               categories=glossary_store.CATEGORIES, languages=LANGUAGES)

    @app.get("/glossaries")
    def legacy_glossaries_page():  # 旧地址
        return redirect(url_for("context_page"), 301)

    # ---- 背景资料 API（沿用 glossaries 的路径）--------------------------
    @app.get("/api/glossaries")
    def api_glossaries():
        return jsonify([g.to_dict() for g in glossary_store.list_all()])

    def _glossary_from_body() -> glossary_store.Glossary:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise glossary_store.GlossaryError("数据格式不正确")
        if (not isinstance(body.get("terms", {}), (dict, list)) or not isinstance(body.get("entries", []), list)
                or not isinstance(body.get("notes", ""), str)):
            raise glossary_store.GlossaryError("数据格式不正确")
        return glossary_store.Glossary.from_dict(body)

    @app.post("/api/glossaries")
    def api_glossary_create():
        try:
            g = _glossary_from_body()
            if glossary_store.path_of(g.id).exists():
                return jsonify(error=f"已经有名为“{g.name}”的背景资料"), 409
            glossary_store.save(g)
        except glossary_store.GlossaryError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(g.to_dict()), 201

    @app.put("/api/glossaries/<gid>")
    def api_glossary_update(gid: str):
        try:
            if not glossary_store.path_of(gid).is_file():
                abort(404)
            g = _glossary_from_body()
            with ExitStack() as reservations:
                jobs = []
                if g.id != gid:
                    for state in Job.list(workspace):
                        if gid in state.glossaries:
                            reservations.enter_context(runner.reserve(state.id))
                            jobs.append(load(state.id))
                glossary_store.save(g, old_id=gid)
                for job in jobs:
                    job.state.glossaries = [g.id if i == gid else i for i in job.state.glossaries]
                    job.save()
        except glossary_store.GlossaryError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(g.to_dict())

    @app.delete("/api/glossaries/<gid>")
    def api_glossary_delete(gid: str):
        try:
            glossary_store.delete(gid)
        except glossary_store.GlossaryError:
            abort(404)
        return jsonify(ok=True)

    # ---- 设置与环境检查 API -------------------------------------------
    @app.get("/api/settings")
    def api_settings():
        try:
            return jsonify(settings_store.describe(config_path))
        except ConfigError as exc:
            return jsonify(error=str(exc)), 500

    @app.put("/api/settings")
    def api_settings_save():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("changes"), dict):
            return jsonify(error="请求数据格式不正确"), 400
        with settings_lock:
            try:
                result = settings_store.save(config_path, body["changes"])
            except (settings_store.SettingsError, ConfigError) as exc:
                return jsonify(error=str(exc)), 400
            except OSError as exc:
                return jsonify(error=f"无法写入配置文件：{exc}"), 500
            apply_settings(result)
            return jsonify(settings=settings_store.describe(config_path), notices=result.notices,
                           path=str(result.path))

    @app.get("/api/doctor")
    def api_doctor():
        return jsonify([c.to_dict() for c in local_checks(runner.cfg, config_path)])

    @app.post("/api/doctor/online")
    def api_doctor_online():
        return jsonify([c.to_dict() for c in online_checks(runner.cfg)])

    # ---- 任务 API ------------------------------------------------------
    @app.get("/api/jobs")
    def api_jobs():
        return jsonify([view(load(s.id)) for s in Job.list(workspace)[:50]])

    def uploaded(name: str):
        file = request.files.get(name)
        return file if file and file.filename else None

    def bad_request(message: str):
        return jsonify(error=message), 400

    @app.post("/api/jobs")
    def api_create():
        """创建任务。
        - mode=media：识别视频（target 为空时只识别不翻译）。视频可以上传，也可以是服务器上的文件（video_ref）；
        - mode=url：下载视频链接后识别（需要 yt-dlp）；
        - mode=subtitles：导入 SRT / VTT 字幕翻译或转换格式，可以附带视频用于预览和烧录。
        服务器文件的引用形如“根目录编号:相对路径”，只能指向允许的目录。
        """
        mode = request.form.get("mode") or "media"
        if mode not in ("media", "url", "subtitles"):
            return bad_request("不支持的任务类型")
        try:
            video_ref = browser.file(request.form["video_ref"], "video") if request.form.get("video_ref") else None
            sub_ref = (browser.file(request.form["subtitles_ref"], "subtitles")
                       if mode == "subtitles" and request.form.get("subtitles_ref") else None)
        except FileAccessError as exc:
            return bad_request(str(exc))
        video = None if video_ref else uploaded("video")
        subtitles = None if sub_ref or mode != "subtitles" else uploaded("subtitles")
        url = ""
        if mode == "url":
            video = video_ref = None
            if not downloader.available(runner.cfg.get("download", {})):
                return bad_request("服务器没有安装 yt-dlp，无法下载视频链接")
            try:
                url = downloader.check_url(request.form.get("url", ""),
                                           bool(runner.cfg.get("download", {}).get("allow_private")))
            except downloader.DownloadError as exc:
                return bad_request(str(exc))
        if mode == "media" and not (video or video_ref):
            return bad_request("请选择视频文件")
        if mode == "subtitles" and not (subtitles or sub_ref):
            return bad_request("请选择字幕文件")
        target = request.form.get("target", "zh")
        if target and target not in LANG_CODES:
            return bad_request("不支持的目标语言")
        source = request.form.get("source") or None
        if source and source not in LANG_CODES:
            return bad_request("不支持的原语言")
        fmt = request.form.get("format") or runner.cfg["subtitle"]["format"]
        if fmt not in FORMATS:
            return bad_request("不支持的字幕格式")
        try:
            glossaries = check_glossaries(request.form.getlist("glossaries"))
        except ValueError as exc:
            return bad_request(str(exc))
        notes = request.form.get("notes", "")[:4000]
        term_mode = request.form.get("terms") or str(runner.cfg.get("terms", {}).get("mode", "off"))
        if term_mode not in TERM_MODES:
            return bad_request("不支持的术语扫描方式")
        segments, raw, sub_suffix = None, b"", ""
        if subtitles or sub_ref:
            sub_suffix = (sub_ref.suffix if sub_ref else Path(subtitles.filename).suffix).lower()
            if sub_suffix not in SUBTITLE_SUFFIXES:
                return bad_request("目前只能导入 SRT 或 VTT 字幕")
            if sub_ref and sub_ref.stat().st_size > MAX_SUBTITLE_BYTES:
                return bad_request("字幕文件太大")
            raw = sub_ref.read_bytes() if sub_ref else subtitles.read()
            try:
                segments = parse_srt(raw.decode("utf-8-sig"))
            except UnicodeDecodeError:
                return bad_request("字幕文件需要是 UTF-8 编码")
            if not segments:
                return bad_request("字幕文件里没有找到带时间轴的字幕")
        tmp = None
        if video:
            suffix = Path(video.filename).suffix.lower()[:8] or ".mp4"
            tmp = workspace / "uploads" / f"{secrets.token_hex(8)}{suffix}"
            video.save(tmp)
        if url:
            name = urlsplit(url).netloc + urlsplit(url).path
        elif video_ref or video:
            name = video_ref.name if video_ref else Path(video.filename).name
        else:
            name = sub_ref.name if sub_ref else Path(subtitles.filename).name
        has_video = bool(tmp or video_ref or url)
        job = Job.create(workspace, tmp or video_ref, target or None, source,
                         bilingual=request.form.get("bilingual") == "true" and bool(target),
                         burn=request.form.get("burn") == "true" and has_video, fmt=fmt,
                         move_video=tmp is not None, glossaries=glossaries, notes=notes, source_url=url,
                         term_mode=term_mode)
        with runner.reserve(job.state.id) as reservation:
            write_json(job.path("meta.json"), {"name": name, **({"url": url} if url else {})})
            if segments is not None:
                job.path("imported" + sub_suffix).write_bytes(raw)
                job.import_subtitles(segments)
            runner.submit(job, reservation)
        return jsonify(id=job.state.id, url=url_for("job_page", job_id=job.state.id)), 201

    @app.get("/api/files")
    def api_files():
        """浏览服务器上允许访问的目录。kind=video / subtitles 时只列出这类文件。"""
        try:
            return jsonify(browser.list(request.args.get("root", 0), request.args.get("path", ""),
                                        request.args.get("kind")))
        except FileAccessError as exc:
            return bad_request(str(exc))

    @app.delete("/api/jobs/<job_id>")
    def api_delete(job_id: str):
        """删除任务目录（含上传的视频）。命令行任务引用的原视频在任务目录之外，不会被删除。"""
        with runner.reserve(job_id):
            job = load(job_id)
            job.path("state.json").unlink()
        shutil.rmtree(job.dir, ignore_errors=True)
        return jsonify(ok=True)

    @app.post("/api/jobs/<job_id>/restart")
    def api_restart(job_id: str):
        """按当前设置从头重新处理（重新识别；导入的字幕从翻译开始），会覆盖校对过的字幕。"""
        with runner.reserve(job_id) as reservation:
            job = load(job_id)
            check_revision(job, edit_body())
            if (job.state.source_type != "subtitles" and not job.state.source_url
                    and not Path(job.state.video).is_file()):
                return bad_request("找不到原视频，无法重新识别")
            job.migrate()
            if job.state.source_type != "subtitles":
                job.state.stats = {}  # 重新识别，费用和统计从头计算
            runner.submit(job, reservation, from_stage=first_stage(job.state))
        return jsonify(view(load(job_id)))

    @app.get("/api/jobs/<job_id>")
    def api_job(job_id: str):
        return jsonify(view(load(job_id)))

    @app.get("/api/jobs/<job_id>/segments")
    def api_segments(job_id: str):
        return jsonify([s.to_dict() for s in load(job_id).segments()])

    def edit_body() -> dict[str, Any]:
        body = request.get_json(silent=True)
        if body is None and not request.data:
            return {}
        if not isinstance(body, dict):
            response = jsonify(error="请求数据格式不正确")
            response.status_code = 400
            abort(response)
        return body

    def check_revision(job: Job, body: dict[str, Any]) -> None:
        if "revision" in body and body["revision"] != job.state.revision:
            raise JobBusyError("任务已更新，请刷新页面后再保存")

    @app.post("/api/jobs/<job_id>/render")
    def api_render(job_id: str):
        """保存修改过的字幕和导出设置，然后从“生成字幕”阶段重新执行。"""
        with runner.reserve(job_id) as reservation:
            job = load(job_id)
            st = job.state
            body = edit_body()
            check_revision(job, body)
            if body.get("format", st.format) not in FORMATS:
                return jsonify(error="不支持的字幕格式"), 400
            if any(k in body and not isinstance(body[k], bool) for k in ("bilingual", "burn")):
                return jsonify(error="导出选项格式不正确"), 400
            if body.get("burn") and not st.video:
                return jsonify(error="这个任务没有视频，不能烧录"), 400
            edited: list[Segment] = []
            if body.get("segments") is not None:
                current = job.segments()  # 待迁移的任务没有可校对的字幕
                items = body["segments"]
                if not isinstance(items, list) or len(items) != len(current):
                    return jsonify(error="字幕条数与原任务不一致"), 400
                if not all(isinstance(i, dict)
                           and all(isinstance(i.get(k, ""), str) for k in ("text", "translation")) for i in items):
                    return jsonify(error="字幕数据格式不正确"), 400
                edited = [Segment(c.start, c.end, i.get("text", c.text), i.get("translation", c.translation), c.group)
                          for c, i in zip(current, items)]
            # 请求全部校验通过后才迁移和写盘，被拒的请求不改动任务。
            job.migrate()
            if edited:
                job.save_segments(edited)
            st.format = body.get("format", st.format)
            st.bilingual = body.get("bilingual", st.bilingual) and bool(st.target_lang)
            st.burn = body.get("burn", st.burn)
            # 已完成识别和翻译的任务只需从生成字幕开始；中断或失败的任务从断点续跑。
            prepared = set(enabled_stages(st)) - {"write", "burn"}
            from_stage = "write" if prepared <= set(st.completed) else None
            runner.submit(job, reservation, from_stage=from_stage)
        return jsonify(view(load(job_id)))

    @app.post("/api/jobs/<job_id>/retranslate")
    def api_retranslate(job_id: str):
        """更换背景资料和补充说明后重新翻译（会覆盖校对过的译文）。"""
        with runner.reserve(job_id) as reservation:
            job = load(job_id)
            if not job.state.target_lang:
                return jsonify(error="这个任务只做识别，没有翻译"), 400
            body = edit_body()
            check_revision(job, body)
            try:
                glossaries = check_glossaries(body.get("glossaries", job.state.glossaries))
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            notes = body.get("notes", job.state.notes)
            if not isinstance(notes, str):
                return jsonify(error="补充说明格式不正确"), 400
            job.migrate()
            job.state.glossaries, job.state.notes = glossaries, notes.strip()[:4000]
            # 背景资料的内容可能改过，即使选择没变也重新翻译。
            runner.submit(job, reservation, from_stage="translate")
        return jsonify(view(load(job_id)))

    # ---- 术语审核 API ---------------------------------------------------
    @app.get("/api/jobs/<job_id>/terms")
    def api_terms(job_id: str):
        """审核数据：实体、出现位置所在句子的原文和时间，以及可以保存到的背景资料。"""
        job = load(job_id)
        review = term_review.load_review(job.dir) if job.state.term_mode != "off" else None
        data: dict[str, Any] = {"mode": job.state.term_mode, "revision": job.state.revision, "review": None,
                                "glossaries": glossary_summaries(), "job_glossaries": job.state.glossaries}
        if review is None:
            return jsonify(data)
        sentences = {}
        if job.path("aligned.json").is_file():
            wanted = {o.sentence_id for e in review.entities for o in e.occurrences}
            sentences = {s.id: {"text": s.text, "start": s.start, "end": s.end}
                         for s in sentences_from_cues(segments_from_list(read_json(job.path("aligned.json"))))
                         if s.id in wanted}
        found = read_json(job.path(term_review.CANDIDATES_FILE)) if job.path(term_review.CANDIDATES_FILE).is_file() else {}
        data.update(review={**review.to_dict(), "summary": review.summary()}, sentences=sentences,
                    invalid=len(found.get("invalid", [])), candidates=len(found.get("candidates", [])))
        return jsonify(data)

    @app.post("/api/jobs/<job_id>/terms")
    def api_terms_save(job_id: str):
        # 在释放任务之后再读取状态：没有重新处理时 busy 为 false，页面不会一直锁住
        error = _terms_save(job_id)
        return error if error is not None else jsonify(view(load(job_id)))

    def _terms_save(job_id: str):
        """保存审核结果。action：
        - save：只保存；
        - continue：结束审核（剩下待审核的不使用）并继续处理；翻译已经完成时重新翻译；
        - scan：（重新）扫描术语，mode 为 review 或 auto；
        - disable：不再使用术语扫描（审核数据保留）；任务停在扫描或审核时继续处理。
        save_to 把通过的实体保存到背景资料：[{key, glossary}]。
        """
        with runner.reserve(job_id) as reservation:
            job = load(job_id)
            body = edit_body()
            check_revision(job, body)
            action = body.get("action", "save")
            if action not in ("save", "continue", "scan", "disable"):
                return bad_request("不支持的操作")
            if not job.state.target_lang:
                return bad_request("这个任务只做识别，没有翻译")
            if action == "scan":
                mode = body.get("mode", "review")
                if mode not in ("review", "auto"):
                    return bad_request("不支持的术语扫描方式")
                job.migrate()
                job.state.term_mode = mode
                runner.submit(job, reservation, from_stage="term_scan")
                return None
            if action == "disable":
                job.migrate()
                stuck = job.state.status in ("error", "awaiting_review")
                job.state.term_mode = "off"
                if stuck:
                    runner.submit(job, reservation)
                else:
                    job.save()
                return None
            review = term_review.load_review(job.dir) if job.state.term_mode != "off" else None
            if review is None:
                return bad_request("这个任务还没有术语扫描结果")
            updates, merges, save_to = body.get("updates", []), body.get("merges", []), body.get("save_to", [])
            if not all(isinstance(x, list) for x in (updates, merges, save_to)):
                return bad_request("审核数据的格式不正确")
            try:
                for update in updates:
                    term_review.apply_update(review, update)
                for m in merges:
                    if not isinstance(m, dict) or not isinstance(m.get("keys"), list):
                        raise term_review.ReviewError("合并数据的格式不正确")
                    term_review.merge(review, str(m.get("into")), [str(k) for k in m["keys"]])
                for item in save_to:
                    if not isinstance(item, dict):
                        raise term_review.ReviewError("保存数据的格式不正确")
                    gid = check_glossaries([item.get("glossary")])[0]
                    term_review.save_to_library(review.find(str(item.get("key"))), gid, job.state.target_lang)
            except (term_review.ReviewError, ValueError) as exc:
                return bad_request(str(exc))
            if action == "continue":
                review.finished = True
            term_review.save_review(job.dir, review)
            if action == "continue":
                job.migrate()
                runner.submit(job, reservation, from_stage="translate" if "translate" in job.state.completed else None)
            else:
                job.save()
        return None

    @app.get("/jobs/<job_id>/files/<name>")
    def job_file(job_id: str, name: str):
        job = load(job_id)
        if name == "source":
            path = Path(job.state.video)
        elif name == "preview":
            path = job.path("preview.vtt")
        else:
            abort(404)
        if not path.is_file():
            abort(404)
        if name == "preview":
            resp = send_file(path, conditional=True, mimetype="text/vtt")
            resp.headers["Cache-Control"] = "no-store"
        else:
            preview_type = media.preview_mimetype(path)
            resp = send_file(path, conditional=True, mimetype=preview_type or "application/octet-stream",
                             as_attachment=preview_type is None)
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        return resp

    @app.get("/jobs/<job_id>/download/<kind>")
    def job_download(job_id: str, kind: str):
        job = load(job_id)
        data = view(job)
        stem = Path(data["name"]).stem or job.state.id
        tag = job.state.target_lang or job.state.source_lang or "orig"
        if kind == "subtitles" and "subtitles" in job.state.outputs:
            path = job.subtitle_path
            return send_file(path, as_attachment=True, download_name=f"{stem}.{tag}.{job.state.format}",
                             mimetype=MEDIA_TYPES.get(job.state.format))
        if kind == "video" and "video" in job.state.outputs and job.path("output.mp4").is_file():
            return send_file(job.path("output.mp4"), as_attachment=True, download_name=f"{stem}.{tag}.mp4")
        abort(404)

    return app


__all__ = ["create_app", "LANGUAGES", "LANG_NAMES"]
