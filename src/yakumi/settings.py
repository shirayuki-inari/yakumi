"""网页设置：可在网页上修改的配置项、各项的来源，以及写回配置文件。

写入规则：
- 写入优先级最高的配置文件：用 -c 指定的文件；否则当前目录有 yakumi.toml 时写入它，没有时写入全局配置。
  这样保存的值一定生效（被环境变量设置的项除外，这些项在网页上只读）。
- 只改动涉及的行，保留注释和其他内容；文件结构复杂、无法安全地逐行修改时，整体重写并保留 .bak 备份。
- 密钥只写不读：网页只知道是否已设置、来自哪里。
- 服务地址换到另一个来源、却没有同时填写新密钥时，删除这一段原来的密钥，避免把旧服务的密钥发给新地址。
- 会执行本地程序或读取任意路径的配置项（whisper.cpp 路径、字体目录）、工作台自身的安全设置（[web]）
  和任务目录只能在配置文件里修改。
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import (DEFAULTS, ENV_MAP, LLM_SECTIONS, SERVICE_KEYS, ConfigError, build_config, config_files,
                     inherited_llm_config, read_config_file, service_origin, user_config_path)

REMOVE = object()


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    id: str  # 段.键
    label: str
    kind: str  # text | textarea | url | secret | int | float | bool | choice
    help: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    min: float | None = None
    max: float | None = None
    placeholder: str = ""
    suggestions: tuple[str, ...] = ()
    required: bool = False
    # 只在另一项满足条件时显示：{"field": "asr.provider", "in": [...]} 或 {"field": "...", "openrouter": True}
    show_if: dict[str, Any] | None = None

    @property
    def section(self) -> str:
        return self.id.split(".", 1)[0]

    @property
    def key(self) -> str:
        return self.id.split(".", 1)[1]


EFFORTS = (("low", "low：快、便宜"), ("medium", "medium：均衡"), ("high", "high：很慢、很贵"), ("none", "none：关闭思考"))
ONLINE_ASR = {"field": "asr.provider", "in": ["openai"]}
LLM_SEGMENT = {"field": "segment.mode", "in": ["llm"]}
SERVICE_URLS = ("https://openrouter.ai/api/v1", "https://api.openai.com/v1", "http://127.0.0.1:11434/v1")

GROUPS: list[dict[str, Any]] = [
    {"id": "asr", "title": "语音识别", "icon": "mic",
     "description": "把视频里的语音转成文字。默认通过 OpenRouter 调用 Gemini 3.5 Transcribe。",
     "fields": [
         Field("asr.provider", "识别方式", "choice", choices=(("openai", "在线服务（OpenAI 兼容接口）"),
                                                       ("whisper_cpp", "本地 whisper.cpp"))),
         Field("asr.base_url", "服务地址", "url", "OpenAI 兼容接口的地址，以 /v1 结尾。", required=True,
               suggestions=SERVICE_URLS[:2], show_if=ONLINE_ASR),
         Field("asr.model", "模型", "text", required=True,
               suggestions=("google/gemini-3.5-transcribe", "whisper-1"), show_if=ONLINE_ASR),
         Field("asr.api_key", "API 密钥", "secret", "OpenRouter 的密钥以 sk-or- 开头。", show_if=ONLINE_ASR),
         Field("asr.mode", "时间戳", "choice", "自动探测会在首次使用时测试一次，并记住结果。",
               choices=(("auto", "自动探测"), ("words", "词级时间戳"), ("text", "纯文本（按语音区间估算时间）")),
               show_if=ONLINE_ASR),
         Field("asr.upload_format", "上传格式", "choice", "网速慢时选 Opus，体积约为 FLAC 的 1/4。",
               choices=(("flac", "FLAC（无损）"), ("opus", "Opus（体积小）")), show_if=ONLINE_ASR),
         Field("asr.target_window", "识别窗口（秒）", "int", "长视频按这个长度在停顿处切开分别识别。",
               min=30, max=1800, show_if=ONLINE_ASR),
         Field("asr.max_window", "窗口上限（秒）", "int", "单次请求的音频不超过这个长度。", min=30, max=3600,
               show_if=ONLINE_ASR),
         Field("asr.timeout", "请求超时（秒）", "int", "服务端单次请求的处理时限，OpenRouter 为 60 秒。",
               min=10, max=1200, show_if=ONLINE_ASR),
         Field("asr.threads", "线程数", "int", min=1, max=64, show_if={"field": "asr.provider", "in": ["whisper_cpp"]}),
     ]},
    {"id": "translate", "title": "翻译", "icon": "languages",
     "description": "把识别出的句子翻译成目标语言。任何 OpenAI 兼容的 /chat/completions 接口都可以使用。",
     "fields": [
         Field("translate.base_url", "服务地址", "url", "本地 Ollama 一般是 http://127.0.0.1:11434/v1，不需要密钥。",
               required=True, suggestions=SERVICE_URLS),
         Field("translate.model", "模型", "text", required=True,
               suggestions=("google/gemini-3.8-flash", "deepseek/deepseek-chat")),
         Field("translate.api_key", "API 密钥", "secret", "使用本地服务时可以不填。"),
         Field("translate.reasoning_effort", "思考档位", "choice",
               "档位越高越慢越贵。Gemini 3.8 Flash 不能关闭思考。",
               choices=(("", "模型默认"), *EFFORTS)),
         Field("translate.routing", "路由", "choice", "OpenRouter 在多个服务商之间的挑选方式。",
               choices=(("default", "默认"), ("floor", "便宜优先（允许 flex 档）"), ("nitro", "速度优先（允许 priority 档）")),
               show_if={"field": "translate.base_url", "openrouter": True}),
         Field("translate.unit", "翻译单位", "choice", "整句翻译再按字幕条分段，语序不同的语言之间更通顺。",
               choices=(("sentence", "整句翻译"), ("cue", "逐条翻译"))),
         Field("translate.batch_sentences", "每批句数", "int", min=1, max=100,
               show_if={"field": "translate.unit", "in": ["sentence"]}),
         Field("translate.batch_size", "每批条数", "int", min=1, max=200,
               show_if={"field": "translate.unit", "in": ["cue"]}),
         Field("translate.concurrency", "同时请求数", "int", "设为 1 时逐批翻译，每批会带上前一批的译文作为上下文。",
               min=1, max=16),
         Field("translate.temperature", "温度", "float", min=0, max=2),
         Field("translate.timeout", "请求超时（秒）", "int", min=10, max=1200),
         Field("translate.style", "译文风格", "text", placeholder="例如：轻松口语化，保留网络流行语"),
         Field("translate.notes", "全局背景说明", "textarea",
               "原样写进每次翻译的提示词。按频道、节目区分的说明建议放进“背景资料”。"),
     ]},
    {"id": "segment", "title": "断句", "icon": "scissors",
     "description": "把识别结果切成完整的句子作为翻译单位，再把每句拆成字幕条。模型只插入换行，不改动文字。",
     "fields": [
         Field("segment.mode", "断句方式", "choice", choices=(("llm", "模型按语义断句"), ("rules", "只按句末标点"))),
         Field("segment.model", "模型", "text", "留空时沿用翻译模型。", show_if=LLM_SEGMENT),
         Field("segment.reasoning_effort", "思考档位", "choice", "断句只需要插入换行，low 就够。",
               choices=(("", "沿用翻译设置"), *EFFORTS), show_if=LLM_SEGMENT),
         Field("segment.base_url", "服务地址", "url", "留空时沿用翻译接口。", suggestions=SERVICE_URLS,
               show_if=LLM_SEGMENT),
         Field("segment.api_key", "API 密钥", "secret", "留空时沿用同一服务的翻译密钥。", show_if=LLM_SEGMENT),
         Field("segment.max_sentence_chars", "每句字数上限", "int", "超过两倍时强制拆开。", min=10, max=500),
         Field("segment.cue_gap", "句内拆分停顿（秒）", "float", "同一句里停顿超过这么久，就拆成新的字幕条。",
               min=0.2, max=10),
     ]},
    {"id": "terms", "title": "术语扫描", "icon": "scan-text",
     "description": "翻译前让模型找出人名、昵称、作品名等（包括被识别写错的），人工审核后统一译名。"
                    "新建任务时可以单独选择；这里设置默认做法和使用的模型。",
     "fields": [
         Field("terms.mode", "新任务默认", "choice", "人工审核时，任务会在翻译前暂停，审核完成后继续。",
               choices=(("off", "不扫描"), ("review", "扫描后人工审核"), ("auto", "扫描，只自动采用与背景资料对应的结果"))),
         Field("terms.model", "模型", "text", "留空时沿用翻译模型。",
               suggestions=("deepseek/deepseek-v4.1-flash-1017", "google/gemini-3.8-flash")),
         Field("terms.reasoning_effort", "思考档位", "choice",
               "默认模型可选 low / high / none，没有 medium。", choices=(("", "沿用翻译设置"), *EFFORTS)),
         Field("terms.base_url", "服务地址", "url", "留空时沿用翻译接口。", suggestions=SERVICE_URLS),
         Field("terms.api_key", "API 密钥", "secret", "留空时沿用同一服务的翻译密钥。"),
         Field("terms.chunk_chars", "每次请求的字数", "int", "长视频按这个长度分块扫描，相邻两块略有重叠。",
               min=500, max=20000),
     ]},
    {"id": "subtitle", "title": "字幕", "icon": "captions",
     "description": "新任务的默认导出设置，以及字幕条的长度和烧录样式。",
     "fields": [
         Field("subtitle.format", "默认格式", "choice", choices=(("srt", "SRT"), ("ass", "ASS"), ("vtt", "VTT"))),
         Field("subtitle.bilingual", "默认双语", "bool", "译文在上、原文在下。"),
         Field("subtitle.punctuation", "标点", "choice",
               "按字幕惯例时，中日文句中的逗号、句号换成空格，行尾的去掉。只影响导出。",
               choices=(("subtitle", "按字幕惯例"), ("keep", "原样保留"))),
         Field("subtitle.max_chars_cjk", "每条字数（中日韩）", "int", min=5, max=100),
         Field("subtitle.max_chars", "每条字数（其他语言）", "int", min=10, max=200),
         Field("subtitle.max_duration", "每条最长（秒）", "float", min=1, max=30),
         Field("subtitle.min_duration", "每条最短（秒）", "float", "更短的字幕会尝试与相邻条合并。", min=0, max=10),
         Field("subtitle.font", "字体", "text", "烧录和 ASS 使用的字体名，留空用系统默认。",
               placeholder="例如：Noto Sans CJK SC"),
         Field("subtitle.font_size", "字号", "int", min=8, max=120),
     ]},
    {"id": "advanced", "title": "长视频与其他", "icon": "sliders",
     "description": "一般不需要修改。",
     "fields": [
         Field("chunking.vad", "语音检测", "choice", "只用来选择切点，不会删除任何音频。没有 onnxruntime 时自动改用能量检测。",
               choices=(("silero", "Silero VAD"), ("energy", "能量检测（ffmpeg）"), ("none", "不检测"))),
         Field("chunking.vad_threshold", "语音概率阈值", "float", min=0.05, max=0.95,
               show_if={"field": "chunking.vad", "in": ["silero"]}),
         Field("chunking.silence_db", "静音阈值（dB）", "int", min=-90, max=-5,
               show_if={"field": "chunking.vad", "in": ["energy"]}),
         Field("chunking.overlap", "切点重叠（秒）", "float", "每个切点两侧各重叠这么久，合并时按时间对齐去重。",
               min=0, max=10),
         Field("repair.enabled", "边界复核", "bool", "切点附近疑似漏字或重复时，截取局部音频重新识别。"),
         Field("repair.max_repairs", "最多复核几处", "int", min=0, max=200,
               show_if={"field": "repair.enabled", "in": [True]}),
         Field("workspace.keep_audio", "保留抽出的音频", "bool", "任务完成后保留 audio.wav（约 115MB/小时）。"),
         Field("download.format", "链接下载格式", "text", "下载视频链接时交给 yt-dlp 的 -f 参数（需要安装 yt-dlp）。",
               required=True, suggestions=("bv*[height<=1080]+ba/b[height<=1080]/b", "bv*[height<=720]+ba/b[height<=720]/b",
                                           "ba/b")),
     ]},
]
FIELDS = {f.id: f for g in GROUPS for f in g["fields"]}

# 只能在配置文件里修改的项（网页只显示）
READONLY = [
    ("asr.whisper_cpp_bin", "whisper.cpp 程序"),
    ("asr.whisper_cpp_model", "whisper.cpp 模型"),
    ("subtitle.fonts_dir", "额外字体目录"),
    ("workspace.dir", "任务目录"),
    ("web.max_upload_mb", "上传大小上限（MB）"),
    ("web.allowed_hosts", "允许的域名"),
    ("web.password_hash", "登录密码"),
    ("web.proxy_count", "反向代理层数"),
    ("web.file_roots", "可浏览的服务器目录"),
    ("download.ytdlp", "yt-dlp 程序"),
    ("download.extra_args", "yt-dlp 额外参数"),
    ("download.allow_private", "允许下载内网链接"),
]
SERVICE_SECTIONS = ("asr", "translate", *LLM_SECTIONS)


def _service_url(cfg: dict[str, Any], section: str) -> str:
    return inherited_llm_config(cfg, section)["base_url"] if section in LLM_SECTIONS else cfg[section]["base_url"]


# ---- 配置文件与来源 -----------------------------------------------------
def config_target(config_path: str | Path | None = None) -> Path:
    """网页保存设置时写入的文件：优先级最高的那一层。"""
    if config_path is not None:
        return Path(config_path)
    project = Path.cwd() / "yakumi.toml"
    return project if project.is_file() else user_config_path()


def file_label(path: Path, config_path: str | Path | None = None) -> str:
    if config_path is not None:
        return "配置文件"
    if path == user_config_path():
        return "全局配置"
    return "项目配置"


def _get(data: dict[str, Any], fid: str) -> Any:
    section, key = fid.split(".", 1)
    value = data.get(section)
    return value.get(key, REMOVE) if isinstance(value, dict) else REMOVE


def _env_locks() -> dict[str, str]:
    return {f"{section}.{key}": env for env, (section, key) in ENV_MAP.items() if os.environ.get(env)}


def _layers(config_path: str | Path | None) -> list[tuple[Path, dict[str, Any]]]:
    return [(path, read_config_file(path)) for path in config_files(config_path)]


def describe(config_path: str | Path | None = None) -> dict[str, Any]:
    """设置页需要的全部信息：分组、每一项的当前值和来源，以及只读项。密钥只返回是否已设置。"""
    layers = _layers(config_path)
    cfg = build_config([data for _, data in layers])
    target = config_target(config_path)
    target_data = next((data for path, data in layers if path == target), {})
    locks = _env_locks()
    inherited = {section: inherited_llm_config(cfg, section) for section in LLM_SECTIONS}
    # 从保存的文件里删除某一项之后会用的值（上一层配置或默认值）
    fallback = build_config([data for path, data in layers if path != target])

    def source_of(fid: str, effective: Any) -> str:
        if fid in locks:
            return f"环境变量 {locks[fid]}"
        for path, data in reversed(layers):
            value = _get(data, fid)
            if value is not REMOVE and value == effective:
                return file_label(path, config_path)
        return "默认"

    groups = []
    for group in GROUPS:
        fields = []
        for f in group["fields"]:
            section, key = f.section, f.key
            item: dict[str, Any] = {
                "id": f.id, "label": f.label, "kind": f.kind, "help": f.help, "choices": [list(c) for c in f.choices],
                "min": f.min, "max": f.max, "placeholder": f.placeholder, "suggestions": list(f.suggestions),
                "required": f.required, "show_if": f.show_if,
                "in_target": _get(target_data, f.id) is not REMOVE,
                "locked": f"由环境变量 {locks[f.id]} 设置，取消该变量后才能在这里修改" if f.id in locks else None,
            }
            if f.kind == "secret":
                effective = (inherited[section] if section in inherited else cfg[section]).get("api_key")
                item["set"] = bool(effective)
                item["value"] = None
                item["source"] = _secret_source(f.id, effective, layers, locks, config_path) if effective else ""
            else:
                item["value"] = cfg[section].get(key, DEFAULTS[section].get(key))
                item["fallback"] = fallback[section].get(key, DEFAULTS[section].get(key))
                item["source"] = source_of(f.id, item["value"])
            if section in inherited and key in ("model", "base_url") and not item["value"]:
                item["placeholder"] = f"沿用翻译设置：{inherited[section].get(key, '')}"
            fields.append(item)
        groups.append({k: v for k, v in group.items() if k != "fields"} | {"fields": fields})

    readonly = []
    for fid, label in READONLY:
        section, key = fid.split(".", 1)
        value = cfg[section].get(key)
        if fid == "web.password_hash":  # 只说明是否设置，不显示哈希
            from .web.auth import PASSWORD_ENV

            value = ("已设置（环境变量 " + PASSWORD_ENV + "）" if os.environ.get(PASSWORD_ENV)
                     else "已设置（用 yakumi passwd 修改）" if value else "未设置（用 yakumi passwd 设置）")
        elif isinstance(value, bool):
            value = "是" if value else "否"
        elif isinstance(value, list):
            value = "、".join(map(str, value)) or "（无）"
        readonly.append({"id": fid, "label": label, "value": "" if value in (None, "") else str(value)})
    return {
        "target": str(target), "target_label": file_label(target, config_path), "target_exists": target.is_file(),
        "files": [{"path": str(path), "label": file_label(path, config_path)} for path, _ in layers],
        "groups": groups, "readonly": readonly,
    }


def _secret_source(fid: str, effective: str, layers: list[tuple[Path, dict[str, Any]]], locks: dict[str, str],
                   config_path: str | Path | None) -> str:
    if fid in locks:
        return f"环境变量 {locks[fid]}"
    for path, data in reversed(layers):
        if _get(data, fid) == effective:
            return file_label(path, config_path)
    section = fid.split(".", 1)[0]
    if section in LLM_SECTIONS:
        return "沿用翻译密钥"
    for env in SERVICE_KEYS.values():
        if os.environ.get(env) == effective:
            return f"环境变量 {env}"
    return "已设置"


# ---- 校验 ---------------------------------------------------------------
def _normalize(f: Field, value: Any) -> Any:
    if f.kind == "bool":
        if not isinstance(value, bool):
            raise SettingsError(f"{f.label}：应为开或关")
        return value
    if f.kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise SettingsError(f"{f.label}：请输入数字")
        try:
            number = float(value)
        except ValueError:
            raise SettingsError(f"{f.label}：请输入数字") from None
        if not math.isfinite(number):
            raise SettingsError(f"{f.label}：请输入数字")
        if f.kind == "int":
            if number != int(number):
                raise SettingsError(f"{f.label}：请输入整数")
            number = int(number)
        if (f.min is not None and number < f.min) or (f.max is not None and number > f.max):
            raise SettingsError(f"{f.label}：应在 {f.min:g} 到 {f.max:g} 之间")
        return number
    if not isinstance(value, str):
        raise SettingsError(f"{f.label}：格式不正确")
    if f.kind == "choice":
        if value not in {c[0] for c in f.choices}:
            raise SettingsError(f"{f.label}：不支持的选项 {value}")
        return value
    value = value.strip() if f.kind != "textarea" else value.strip("\n").rstrip()
    limit = 4000 if f.kind == "textarea" else 500
    if len(value) > limit:
        raise SettingsError(f"{f.label}：太长了（最多 {limit} 字）")
    if f.kind != "textarea" and any(ch in value for ch in "\r\n"):
        raise SettingsError(f"{f.label}：不能包含换行")
    if f.kind == "secret":
        if not value or any(ch.isspace() for ch in value):
            raise SettingsError(f"{f.label}：密钥不能为空，也不能包含空格")
    if f.required and not value:
        raise SettingsError(f"{f.label}：不能为空")
    if f.kind == "url" and value and service_origin(value) is None:
        raise SettingsError(f"{f.label}：请输入以 http:// 或 https:// 开头的地址")
    return value


def _check_combined(cfg: dict[str, Any]) -> None:
    asr, sub = cfg["asr"], cfg["subtitle"]
    if float(asr["max_window"]) < float(asr["target_window"]):
        raise SettingsError("识别窗口上限不能小于识别窗口")
    if float(sub["min_duration"]) >= float(sub["max_duration"]):
        raise SettingsError("字幕每条最短时长应小于最长时长")


@dataclass
class SaveResult:
    path: Path
    notices: list[str] = field(default_factory=list)
    cfg: dict[str, Any] = field(default_factory=dict)
    changed: list[str] = field(default_factory=list)  # 实际写入或删除的“段.键”


def save(config_path: str | Path | None, changes: dict[str, Any]) -> SaveResult:
    """校验并保存设置。`changes` 的键为“段.键”，值为 None 时从配置文件中删除这一项（恢复默认或上一层的值）。"""
    if not isinstance(changes, dict) or not changes:
        raise SettingsError("没有需要保存的修改")
    locks = _env_locks()
    updates: dict[str, Any] = {}
    for fid, value in changes.items():
        f = FIELDS.get(fid) if isinstance(fid, str) else None
        if f is None:
            raise SettingsError(f"不能在网页上修改 {fid}")
        if fid in locks:
            raise SettingsError(f"{f.label} 由环境变量 {locks[fid]} 设置，不能在这里修改")
        updates[fid] = REMOVE if value is None else _normalize(f, value)

    target = config_target(config_path)
    layers = _layers(config_path)
    old_text = target.read_text(encoding="utf-8") if target.is_file() else ""
    old_data = tomllib.loads(old_text) if old_text else {}
    others = [data for path, data in layers if path != target]
    before = build_config([*others, old_data])
    result = SaveResult(target)

    # 服务地址换了来源而没有同时填写新密钥时，删除这一段原来的密钥
    after_data = _apply(old_data, updates)
    after = build_config([*others, after_data])
    for section in SERVICE_SECTIONS:
        key_id = f"{section}.api_key"
        if key_id in updates:
            continue
        old_url, new_url = _service_url(before, section), _service_url(after, section)
        if service_origin(old_url) != service_origin(new_url) and _get(old_data, key_id) is not REMOVE:
            updates[key_id] = REMOVE
            title = next(g["title"] for g in GROUPS if g["id"] == section)
            result.notices.append(f"{title}的服务地址变了，已删除原来的密钥，请为新服务填写密钥")
    after_data = _apply(old_data, updates)
    try:
        after = build_config([*others, after_data])
    except (ConfigError, ValueError, TypeError) as exc:
        raise SettingsError(str(exc)) from exc
    _check_combined(after)

    result.notices += _write_toml(target, old_text, updates, after_data)
    result.cfg, result.changed = after, list(updates)
    return result


def write_values(config_path: str | Path | None, values: dict[str, Any]) -> Path:
    """直接写入配置项（不经过网页可修改项的检查，供 yakumi passwd 等命令使用）。值为 None 时删除。"""
    target = config_target(config_path)
    old_text = target.read_text(encoding="utf-8") if target.is_file() else ""
    old_data = tomllib.loads(old_text) if old_text else {}
    updates = {fid: REMOVE if value is None else value for fid, value in values.items()}
    _write_toml(target, old_text, updates, _apply(old_data, updates))
    return target


def _write_toml(target: Path, old_text: str, updates: dict[str, Any], after_data: dict[str, Any]) -> list[str]:
    """逐行修改后核对；无法逐行修改（多行值、内联表等）时整体重写并备份原文件。返回需要告诉用户的提示。"""
    notices = []
    text = patch_toml(old_text, updates)
    try:
        ok = tomllib.loads(text) == after_data
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:
        backup = target.with_name(target.name + ".bak")
        if old_text:
            _write_private(backup, old_text, like=target)
            notices.append(f"配置文件结构较复杂，已整体重写，原文件备份为 {backup}")
        text = f"# Yakumi 配置（已整体重写，原文件备份为 {backup.name}）\n\n" + dump_toml(after_data)
    _write_private(target, text, like=target)
    return notices


def _apply(data: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(data)
    for fid, value in updates.items():
        section, key = fid.split(".", 1)
        if value is REMOVE:
            if isinstance(data.get(section), dict):
                data[section].pop(key, None)
        else:
            data.setdefault(section, {})[key] = value
    return data


def _write_private(path: Path, text: str, like: Path) -> None:
    """原子写入。新文件只有自己可读（可能包含密钥），已有文件保留原来的权限。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (like.stat().st_mode & 0o777) if like.is_file() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


# ---- TOML 读写 ----------------------------------------------------------
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_HEADER = re.compile(r"\s*\[\s*([A-Za-z0-9_-]+(?:\s*\.\s*[A-Za-z0-9_-]+)*)\s*\]\s*(?:#.*)?")
_KEY_LINE = re.compile(r"\s*([A-Za-z0-9_-]+)\s*=")


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{toml_key(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"无法写入配置的值：{value!r}")


def toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else toml_value(key)


def dump_toml(data: dict[str, Any]) -> str:
    out: list[str] = []

    def table(path: list[str], body: dict[str, Any]) -> None:
        scalars = {k: v for k, v in body.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in body.items() if isinstance(v, dict)}
        if path and (scalars or not tables):
            out.append(f"[{'.'.join(toml_key(p) for p in path)}]")
        out.extend(f"{toml_key(k)} = {toml_value(v)}" for k, v in scalars.items())
        if path and (scalars or not tables):
            out.append("")
        for k, v in tables.items():
            table([*path, k], v)

    table([], data)
    return "\n".join(out).rstrip("\n") + "\n"


def _value_end(lines: list[str], start: int) -> int:
    """从 start 行开始的“键 = 值”占到哪一行（不含）。多行数组、多行字符串会跨行。"""
    for end in range(start + 1, min(len(lines), start + 200) + 1):
        try:
            tomllib.loads("".join(lines[start:end]))
            return end
        except tomllib.TOMLDecodeError:
            continue
    return start + 1


def _inline_comment(line: str) -> str:
    """单行“键 = 值  # 注释”里的注释部分（含前面的空白），没有时返回空字符串。"""
    body = line.rstrip("\n")
    try:
        whole = tomllib.loads(body)
    except tomllib.TOMLDecodeError:
        return ""
    for pos in (i for i, ch in enumerate(body) if ch == "#"):
        try:
            if tomllib.loads(body[:pos]) == whole:
                head = body[:pos].rstrip()
                return body[len(head):]
        except tomllib.TOMLDecodeError:
            continue
    return ""


def patch_toml(text: str, updates: dict[str, Any]) -> str:
    """只修改涉及的“段.键”所在的行，保留其他内容和注释。值为 REMOVE 时删除这一行。

    不处理点号键、内联表等写法；调用方需要解析结果并核对，不一致时改为整体重写。
    """
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    spans: dict[str, tuple[int, int]] = {}
    section_end: dict[str, int] = {}
    section: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        header = _HEADER.fullmatch(line.rstrip("\n"))
        if line.lstrip().startswith("[["):
            section = None
        elif header:
            section = re.sub(r"\s+", "", header.group(1))
            section_end.setdefault(section, i + 1)
        else:
            key = _KEY_LINE.match(line)
            if key:
                end = _value_end(lines, i)
                if section is not None:
                    spans[f"{section}.{key.group(1)}"] = (i, end)
                    section_end[section] = end
                i = end
                continue
        i += 1

    replace: dict[int, tuple[int, list[str]]] = {}
    inserts: dict[int, list[str]] = {}
    appended: dict[str, list[str]] = {}
    for fid, value in updates.items():
        sec, key = fid.split(".", 1)
        if fid in spans:
            start, end = spans[fid]
            if value is REMOVE:
                replace[start] = (end, [])
            else:
                comment = _inline_comment(lines[start]) if end == start + 1 else ""
                replace[start] = (end, [f"{toml_key(key)} = {toml_value(value)}{comment}\n"])
        elif value is REMOVE:
            continue
        elif sec in section_end:
            inserts.setdefault(section_end[sec], []).append(f"{toml_key(key)} = {toml_value(value)}\n")
        else:
            appended.setdefault(sec, []).append(f"{toml_key(key)} = {toml_value(value)}\n")

    out: list[str] = []
    i = 0
    while i <= len(lines):
        out.extend(inserts.get(i, []))
        if i == len(lines):
            break
        if i in replace:
            end, new = replace[i]
            out.extend(new)
            i = end
            continue
        out.append(lines[i])
        i += 1
    for sec, body in appended.items():
        if out and out[-1].strip():
            out.append("\n")
        out.append(f"[{sec}]\n")
        out.extend(body)
    return "".join(out)


__all__ = ["FIELDS", "GROUPS", "REMOVE", "SettingsError", "config_target", "describe", "dump_toml", "patch_toml",
           "save"]
