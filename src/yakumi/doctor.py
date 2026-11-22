"""运行环境与配置检查。命令行 `yakumi doctor` 和网页设置页共用同一套检查。"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import media
from .config import config_files, is_local_service, is_openrouter

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


@dataclass
class Check:
    """一项检查。`status` 为 ok / warn / fail / info；`hint` 只在没通过时有意义；`detail` 是补充说明。"""

    status: str
    title: str
    hint: str = ""
    detail: list[str] = field(default_factory=list)
    group: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _check(passed: bool, title: str, hint: str = "", *, warn: bool = False, group: str = "") -> Check:
    status = OK if passed else (WARN if warn else FAIL)
    return Check(status, title, "" if passed else hint, group=group)


def local_checks(cfg: dict[str, Any], config_path: str | Path | None = None) -> list[Check]:
    """不联网的检查：ffmpeg、配置文件、VAD、各服务的密钥和本地程序。"""
    env = "运行环境"
    checks = [
        _check(bool(shutil.which("ffmpeg")), "ffmpeg", "请安装 ffmpeg（Termux：pkg install ffmpeg）", group=env),
        _check(bool(shutil.which("ffprobe")), "ffprobe", "ffprobe 通常随 ffmpeg 一起安装", group=env),
        _check(media.ffmpeg_has_libass(), "ffmpeg 支持字幕烧录（libass）", "烧录字幕需要带 libass 的 ffmpeg",
               group=env),
    ]
    found = config_files(config_path)
    checks.append(Check(INFO, f"配置文件：{', '.join(map(str, found)) if found else '未找到，使用默认值和环境变量'}",
                        group=env))

    from .chunking.vad import SILERO_VERSION, model_cache_dir, silero_available

    vad = cfg["chunking"].get("vad", "silero")
    if vad == "silero":
        if silero_available():
            cached = (model_cache_dir() / f"silero_vad_{SILERO_VERSION}.onnx").is_file()
            checks.append(Check(OK, f"VAD：Silero {SILERO_VERSION}" + ("" if cached else "（模型将在首次使用时下载，约 2MB）"),
                                group=env))
        else:
            checks.append(Check(WARN, "VAD：没有 onnxruntime，将改用能量检测（背景音乐较强时切点可能不准）",
                                "Termux：pkg install python-onnxruntime；其他平台：pip install 'yakumi[vad]'", group=env))
    else:
        checks.append(Check(INFO, f"VAD：{vad}", group=env))

    checks.extend(_asr_checks(cfg["asr"]))
    checks.extend(_translate_checks(cfg))
    checks.extend(_segment_checks(cfg))
    checks.extend(_terms_checks(cfg))
    checks.extend(_web_checks(cfg))
    punct = cfg["subtitle"].get("punctuation", "subtitle")
    checks.append(Check(INFO, "字幕标点：" + ("按字幕惯例（中日文句中逗号句号换成空格，行尾的去掉）"
                                          if punct == "subtitle" else "原样保留"), group="字幕"))
    return checks


def _web_checks(cfg: dict[str, Any]) -> list[Check]:
    """可选组件和网页工作台的部署设置。"""
    import importlib.util
    import os

    from .download import ytdlp_command
    from .web.auth import PASSWORD_ENV

    group = "可选组件与部署"
    ytdlp = ytdlp_command(cfg.get("download", {}))
    checks = [
        Check(OK, "yt-dlp：" + ("Python 模块" if ytdlp[-1] == "yt_dlp" else ytdlp[0]) + "（可以下载视频链接）",
              group=group)
        if ytdlp else
        Check(INFO, "yt-dlp：未安装，不能下载视频链接（可选）", group=group,
              detail=["需要时安装：pip install 'yakumi[ytdlp]'"]),
        Check(OK, "waitress：yakumi serve 将使用它", group=group)
        if importlib.util.find_spec("waitress") is not None else
        Check(INFO, "waitress：未安装，yakumi serve 使用 Flask 自带的服务器（可选）", group=group,
              detail=["部署到服务器时建议安装：pip install 'yakumi[server]'"]),
    ]
    web = cfg.get("web", {})
    if os.environ.get(PASSWORD_ENV):
        checks.append(Check(OK, f"网页登录密码：已设置（环境变量 {PASSWORD_ENV}）", group=group))
    elif web.get("password_hash"):
        checks.append(Check(OK, "网页登录密码：已设置", group=group))
    else:
        checks.append(Check(INFO, "网页登录密码：未设置，只能在本机或加 --no-auth 时使用", group=group,
                            detail=["部署到服务器前运行 yakumi passwd"]))
    return checks


def _asr_checks(asr: dict[str, Any]) -> list[Check]:
    group = "语音识别"
    if asr["provider"] == "whisper_cpp":
        binary = Path(asr["whisper_cpp_bin"]).expanduser()
        model = Path(asr["whisper_cpp_model"]).expanduser() if asr["whisper_cpp_model"] else None
        return [
            Check(INFO, "语音识别：whisper_cpp（本地）", group=group),
            _check(bool(shutil.which(str(binary)) or binary.is_file()), f"whisper.cpp 程序：{binary}",
                   "在配置文件的 [asr] whisper_cpp_bin 中填写 whisper-cli 的路径", group=group),
            _check(bool(model and model.is_file()), f"whisper.cpp 模型：{model or '(未配置)'}",
                   "在配置文件的 [asr] whisper_cpp_model 中填写 ggml 模型文件的路径", group=group),
        ]
    if asr["provider"] != "openai":
        return [Check(INFO, f"语音识别：{asr['provider']}", group=group)]
    info = Check(INFO, f"语音识别：{asr['provider']}", group=group, detail=[
        f"{asr['base_url']}  模型 {asr['model']}",
        f"窗口 {asr['target_window']} 秒（上限 {asr['max_window']} 秒），上传格式 {asr.get('upload_format', 'flac')}",
    ])
    from .asr import create_asr
    from .chunking.transcribe import cached_capability

    try:
        key = create_asr(asr).cache_key  # type: ignore[attr-defined]  # 只有 OpenAI 兼容后端有探测缓存
        entry = cached_capability(key)
    except (OSError, ValueError, AttributeError):
        entry = None
    if entry:
        mode = {"words": "词级时间戳", "text": "纯文本"}.get(entry["mode"], entry["mode"])
        info.detail.append(f"识别能力：{mode}（{entry.get('checked_at', '')} 探测）")
    local = is_local_service(asr["base_url"])
    return [info, _check(bool(asr.get("api_key")) or local, "语音识别 API Key",
                         "设置 OPENROUTER_API_KEY（或 YAKUMI_ASR_API_KEY），或在设置中填写", group=group)]


def _translate_checks(cfg: dict[str, Any]) -> list[Check]:
    group = "翻译"
    tr = cfg["translate"]
    unit = "整句翻译" if tr.get("unit", "sentence") == "sentence" else "逐条翻译"
    detail = f"{unit}，思考档位 {tr.get('reasoning_effort') or '模型默认'}，并发 {tr.get('concurrency', 1)}"
    if is_openrouter(tr["base_url"]):
        detail += f"，路由 {tr.get('routing', 'default')}"
    if tr.get("provider", "openai") != "openai":
        return [Check(INFO, f"翻译：{tr['provider']}", group=group)]
    return [
        Check(INFO, f"翻译：{tr['base_url']}  模型 {tr['model']}", group=group, detail=[detail]),
        _check(bool(tr.get("api_key")) or is_local_service(tr["base_url"]), "翻译 API Key",
               "设置 OPENROUTER_API_KEY（或 YAKUMI_TRANSLATE_API_KEY），或在设置中填写；本地服务可以不填",
               group=group),
    ]


def _segment_checks(cfg: dict[str, Any]) -> list[Check]:
    from .segment import segment_llm_config

    group = "断句"
    if cfg.get("segment", {}).get("mode", "llm") != "llm":
        return [Check(INFO, "断句：规则（只按句末标点）", group=group)]
    seg = segment_llm_config(cfg)
    if seg.get("provider", "openai") != "openai":
        return [Check(INFO, f"断句：LLM（{seg.get('provider')}）", group=group)]
    return [
        Check(INFO, f"断句：LLM，模型 {seg['model']}，思考档位 {seg.get('reasoning_effort') or '模型默认'}",
              group=group),
        _check(bool(seg.get("api_key")) or is_local_service(seg["base_url"]), "断句可以调用模型",
               "没有密钥，将改用规则断句（只按句末标点）", warn=True, group=group),
    ]


def _terms_checks(cfg: dict[str, Any]) -> list[Check]:
    from .terms import terms_llm_config

    group = "术语扫描"
    mode = cfg.get("terms", {}).get("mode", "off")
    label = {"off": "新任务默认不扫描", "review": "新任务默认扫描后人工审核", "auto": "新任务默认扫描并自动采用已知术语"}
    lcfg = terms_llm_config(cfg)
    checks = [Check(INFO, f"术语扫描：{label.get(mode, mode)}，模型 {lcfg.get('model')}", group=group)]
    if lcfg.get("provider", "openai") == "openai":
        checks.append(_check(bool(lcfg.get("api_key")) or is_local_service(lcfg["base_url"]), "术语扫描可以调用模型",
                             "没有密钥，启用术语扫描的任务会失败", warn=mode == "off", group=group))
    return checks


def setup_problems(cfg: dict[str, Any]) -> list[Check]:
    """会让任务直接失败的配置问题（不检查 ffmpeg 以外的运行环境，也不联网），用于网页首页的提醒。"""
    checks = [_check(bool(shutil.which("ffmpeg")), "没有找到 ffmpeg", "请安装 ffmpeg", group="运行环境")]
    checks += [c for c in _asr_checks(cfg["asr"]) + _translate_checks(cfg) if c.status != INFO]
    return [c for c in checks if c.failed]


def online_checks(cfg: dict[str, Any]) -> list[Check]:
    """联网检查：接口能否连通、密钥是否有效；OpenRouter 上再核对模型和思考档位。"""
    import httpx

    from .segment import segment_llm_config

    checks: list[Check] = []
    tr = cfg["translate"]
    targets = []
    if cfg["asr"]["provider"] == "openai":
        targets.append(("语音识别", cfg["asr"]["base_url"], cfg["asr"].get("api_key")))
    if tr.get("provider", "openai") == "openai":
        targets.append(("翻译", tr["base_url"], tr.get("api_key")))
    for name, base, key in targets:
        # OpenRouter 的 /key 能同时验证密钥；其他兼容服务用 /models
        path = "/key" if is_openrouter(base) else "/models"
        try:
            resp = httpx.get(base.rstrip("/") + path, timeout=15,
                             headers={"Authorization": f"Bearer {key}"} if key else {})
            checks.append(_check(resp.status_code < 400, f"{name}接口连通，密钥有效（HTTP {resp.status_code}）",
                                 resp.text[:200], group=name))
        except httpx.HTTPError as exc:
            checks.append(Check(FAIL, f"{name}接口连通", str(exc) or type(exc).__name__, group=name))
    if tr.get("provider", "openai") == "openai" and is_openrouter(tr["base_url"]):
        checks.append(openrouter_model_check(tr, "翻译", "translate.reasoning_effort"))
        seg = segment_llm_config(cfg)
        if cfg.get("segment", {}).get("mode", "llm") == "llm" and is_openrouter(seg["base_url"]):
            checks.append(openrouter_model_check(seg, "断句", "segment.reasoning_effort"))
    from .terms import terms_llm_config

    terms = terms_llm_config(cfg)
    if terms.get("provider", "openai") == "openai" and is_openrouter(terms["base_url"]):
        checks.append(openrouter_model_check(terms, "术语扫描", "terms.reasoning_effort"))
    return checks


def openrouter_model_check(tr: dict[str, Any], name: str = "翻译",
                           effort_key: str = "translate.reasoning_effort") -> Check:
    """用 OpenRouter 的模型列表核对模型是否存在、思考档位是否受支持。"""
    import httpx

    base_model = tr["model"].split(":", 1)[0]
    try:
        resp = httpx.get(tr["base_url"].rstrip("/") + "/models", timeout=20)
        models = {m["id"]: m for m in resp.json().get("data", [])}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return Check(WARN, "读取 OpenRouter 模型列表", str(exc), group=name)
    model = models.get(base_model)
    if not model:
        return Check(FAIL, f"{name}模型 {base_model} 在 OpenRouter 上存在", "请检查模型名", group=name)
    reasoning = model.get("reasoning") or {}
    efforts = reasoning.get("supported_efforts") or []
    effort = (tr.get("reasoning_effort") or "").lower()
    desc = f"支持的思考档位 {', '.join(efforts) or '无'}" + ("（不能关闭）" if reasoning.get("mandatory") else "")
    if not effort:
        return Check(OK, f"{name}模型存在，{desc}", group=name)
    if effort == "none":
        return _check(not reasoning.get("mandatory"), f"{name}思考档位 none，{desc}",
                      "该模型不能关闭思考，请改为 " + " / ".join(efforts), group=name)
    if efforts:
        return _check(effort in efforts, f"{name}思考档位 {effort}，{desc}",
                      f"请把 {effort_key} 改为 " + " / ".join(efforts), group=name)
    return Check(WARN, f"{name}思考档位 {effort}，但该模型没有声明支持的档位", "可能会被忽略或报错", group=name)


__all__ = ["Check", "local_checks", "online_checks", "openrouter_model_check", "setup_problems"]
