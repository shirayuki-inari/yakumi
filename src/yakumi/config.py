"""配置加载。

优先级（后者覆盖前者）：内置默认值 → ~/.config/yakumi/config.toml →
当前目录 ./yakumi.toml → 环境变量 → 命令行参数。
用 -c 指定配置文件时，只读取这一个文件，代替全局配置和项目配置。

任务目录 [workspace] dir 总是解析成绝对路径，不随运行命令时的当前目录变化：
留空时使用 ~/.local/share/yakumi（或 $XDG_DATA_HOME/yakumi）；配置文件里的相对路径
相对于该配置文件所在的目录；环境变量里的相对路径相对于当前目录。
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .files import config_dir, data_dir

DEFAULTS: dict[str, Any] = {
    "asr": {
        "provider": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "google/gemini-3.5-transcribe",
        "api_key": "",
        "mode": "auto",  # auto | words | text
        "timeout": 60,  # 服务端单次请求超时（秒）
        "target_window": 300,  # 词级模式的目标窗口（秒）
        "max_window": 480,  # 单次请求的硬上限（秒）
        "upload_format": "flac",  # flac（无损）| opus（体积约为 1/4，适合网速慢时）
        "whisper_cpp_bin": "whisper-cli",
        "whisper_cpp_model": "",
        "threads": 4,
    },
    "translate": {
        "provider": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "google/gemini-3.8-flash",
        "api_key": "",
        "batch_size": 30,
        "temperature": 0.2,
        # 思考档位：留空不发送（用模型默认）。gemini-3.8-flash 不能关闭思考，可选 low / medium / high。
        # 实测 30 条日语字幕：low 约 4 秒 $0.001；medium 14～45 秒 $0.004～0.006；high 约 52 秒 $0.03
        "reasoning_effort": "medium",
        "routing": "default",  # default | floor（允许便宜的 flex 档）| nitro（允许快的 priority 档），仅 OpenRouter
        # OpenRouter 的 provider 路由对象，作为请求里的 provider 字段原样透传（order / only / ignore / sort 等）。
        # 注意与上面的 provider（翻译后端类型）不是一回事
        "provider_routing": {},
        "concurrency": 4,  # 同时请求的批数；1 表示逐批进行，上下文带上一批译文
        "timeout": 180,  # 单次请求超时（秒）；high 档单批可能接近 1 分钟
        "unit": "sentence",  # sentence（整句翻译，译文按字幕条分段）| cue（逐条翻译）
        "batch_sentences": 15,  # 整句翻译时每批的句子数（每批另外限制在 40 条字幕以内）
        "style": "",
        "notes": "",  # 对所有任务生效的背景说明；按频道、节目区分的放进背景资料（yakumi context）
        "glossary": {},
    },
    "segment": {
        "mode": "llm",  # llm（模型按语义断句，文字不变）| rules（只按句末标点断句）
        # 下面这些留空时沿用 [translate] 的设置：provider / base_url / model / api_key / routing / provider_routing
        "model": "",
        "reasoning_effort": "low",  # 断句只需要插入换行，low 就够
        "temperature": 0,
        "chunk_chars": 400,  # 每次请求的文本长度（字）
        "max_sentence_chars": 60,  # 希望每句不超过的字数；超过两倍时强制拆开
        "concurrency": 4,
        "cue_gap": 1.2,  # 同一句内，停顿超过这么多秒就拆成新的字幕条
    },
    "terms": {
        # 术语扫描：翻译前让模型找出识别文本里的人名、昵称、作品名等，人工审核后统一译名
        "mode": "off",  # 新任务的默认做法：off（不扫描）| review（扫描后暂停，等人工审核）| auto（只自动采用与背景资料对应的结果）
        # 下面这些留空时沿用 [translate] 的设置：provider / base_url / api_key / routing / provider_routing 等
        "model": "deepseek/deepseek-v4.1-flash-1017",
        # 这个模型的思考档位为 max / high / low / none（没有 medium，不能沿用翻译的 medium）
        "reasoning_effort": "low",
        "temperature": 0,
        "chunk_chars": 4000,  # 每次请求的文本长度（字）
        "overlap_chars": 200,  # 相邻两块重叠的长度（字）
        "concurrency": 4,
    },
    "subtitle": {
        "max_chars": 42,
        "max_chars_cjk": 20,
        "max_duration": 6.0,
        "min_duration": 0.8,
        "bilingual": False,
        "format": "srt",
        "font": "",
        "fonts_dir": "",
        "font_size": 18,
        "punctuation": "subtitle",  # subtitle（中日文按字幕惯例：句中逗号句号换成空格，行尾的去掉）| keep
    },
    "chunking": {
        "vad": "silero",  # silero | energy | none（没有 onnxruntime 时自动改用 energy）
        "vad_threshold": 0.5,  # Silero 的语音概率阈值
        "tolerance": 0.3,  # 在目标窗口的 ±30% 范围内找切点
        "text_window": 25,  # 纯文本模式的窗口上限（秒）
        "min_window": 15,  # 超时拆分时的最短窗口（秒）
        "min_gap": 0.3,  # 可作为切点的最短静音（秒）
        "overlap": 1.5,  # 每个切点两侧的重叠（秒），合并时按时间对齐去重
        "edge_margin": 0.5,  # 片段边缘这么多秒内的词可能是被截断的半个音，不作为风险依据（秒）
        "match_tolerance": 0.5,  # 重叠区合并时，两个词时间相差不超过该值才算同一个词（秒）
        "truncation_slack": 3.0,  # 末尾仍有语音、但最后一个词提前超过该值时判定为截断（秒）
        "silence_db": -35,  # energy VAD 的静音阈值（dB）
    },
    "repair": {
        "enabled": True,
        "context": 6.0,  # 复核时截取切点前后各多少秒
        "max_repairs": 20,  # 每个任务最多复核几处
        "min_gap_seconds": 0.8,  # 有语音却连续这么久没有词，视为疑似漏字
    },
    "alignment": {
        "backend": "none",  # 强制对齐后端；目前只有 none（直接使用识别返回的时间戳）
    },
    "web": {
        "max_upload_mb": 4096,  # 上传文件大小上限（MB），0 表示不限制
        "allowed_hosts": [],  # 用域名访问工作台时，把域名加在这里（IP 地址和 localhost 总是允许）
        "password_hash": "",  # 登录密码的哈希，用 yakumi passwd 设置；设置后访问工作台需要登录
        "session_days": 30,  # 勾选“保持登录”时，登录状态保留的天数
        "proxy_count": 0,  # 工作台前面有几层反向代理（nginx、Caddy 等）；大于 0 时信任 X-Forwarded-* 头
        "file_roots": [],  # 除任务目录外，允许在网页上浏览和选择文件的服务器目录
    },
    "download": {
        # 视频链接（需要安装 yt-dlp）。默认最高 1080p，音视频合并为 MP4
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "ytdlp": "",  # yt-dlp 程序路径；留空时自动查找（Python 模块或 PATH 里的 yt-dlp）
        "extra_args": [],  # 额外传给 yt-dlp 的参数，例如 ["--cookies", "~/cookies.txt"]
        "allow_private": False,  # 是否允许下载局域网、本机地址的链接
        "max_duration_seconds": 3600,  # 单个链接任务的总下载时限
        "max_size_mb": 4096,  # 当前任务的下载文件与部分文件总量上限
    },
    "workspace": {
        "dir": "",  # 留空时为 ~/.local/share/yakumi
        "keep_audio": False,  # 任务完成后是否保留抽出的 audio.wav（约 115MB/小时）
    },
}

ENV_MAP = {
    "YAKUMI_ASR_PROVIDER": ("asr", "provider"),
    "YAKUMI_ASR_MODE": ("asr", "mode"),
    "YAKUMI_ASR_BASE_URL": ("asr", "base_url"),
    "YAKUMI_ASR_MODEL": ("asr", "model"),
    "YAKUMI_ASR_API_KEY": ("asr", "api_key"),
    "YAKUMI_TRANSLATE_BASE_URL": ("translate", "base_url"),
    "YAKUMI_TRANSLATE_MODEL": ("translate", "model"),
    "YAKUMI_TRANSLATE_API_KEY": ("translate", "api_key"),
    "YAKUMI_WORKSPACE": ("workspace", "dir"),
}

SERVICE_KEYS = {
    ("https", "openrouter.ai", 443): "OPENROUTER_API_KEY",
    ("https", "api.openai.com", 443): "OPENAI_API_KEY",
}


def service_origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme in ("http", "https") and parsed.hostname and parsed.username is None:
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
            return parsed.scheme, parsed.hostname, port
    except ValueError:
        pass
    return None


def is_openrouter(url: str) -> bool:
    return service_origin(url) == ("https", "openrouter.ai", 443)


# 断句、术语扫描沿用 [translate] 的连接设置；密钥只在同一服务来源下继承
LLM_INHERITED = ("provider", "base_url", "model", "api_key", "routing", "provider_routing", "timeout",
                 "reasoning_effort", "temperature")
LLM_SECTIONS = ("segment", "terms")


def inherited_llm_config(cfg: dict[str, Any], section: str) -> dict[str, Any]:
    """[segment]、[terms] 这类调用对话模型的段：留空的连接设置沿用 [translate]。

    换了服务地址（不同来源）时不继承翻译的密钥，避免把密钥发给别的服务。
    """
    own = cfg.get(section, {})
    merged = dict(cfg.get("translate", {}))
    upstream = service_origin(merged.get("base_url", ""))
    if own.get("base_url") and (upstream is None or service_origin(own["base_url"]) != upstream):
        merged.pop("api_key", None)
    for key in LLM_INHERITED:
        if own.get(key) not in (None, "", {}):
            merged[key] = own[key]
    return merged


def is_local_service(url: str) -> bool:
    origin = service_origin(url)
    return origin is not None and origin[1] in ("localhost", "127.0.0.1", "::1")


def user_config_path() -> Path:
    return config_dir() / "config.toml"


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict) and key not in ("glossary", "provider_routing"):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _service_origins(cfg: dict[str, Any]) -> dict[str, tuple[str, str, int] | None]:
    translate_url = cfg["translate"].get("base_url", "")
    return {
        "asr": service_origin(cfg["asr"].get("base_url", "")),
        "translate": service_origin(translate_url),
        "segment": service_origin(cfg["segment"].get("base_url") or translate_url),
        "terms": service_origin(cfg["terms"].get("base_url") or translate_url),
    }


def merge_config_layer(cfg: dict[str, Any], layer: dict[str, Any]) -> None:
    """叠加一层配置。服务来源变化时清除上一层的对应密钥。"""
    previous = _service_origins(cfg)
    _deep_merge(cfg, layer)
    for section, origin in _service_origins(cfg).items():
        if origin != previous[section] and "api_key" not in layer.get(section, {}):
            cfg[section].pop("api_key", None)


# 这些配置项的值是自由的键值表，不检查里面的键名
FREE_TABLES = {("translate", "glossary"), ("translate", "provider_routing"), ("segment", "provider_routing"),
               ("terms", "provider_routing")}


class ConfigError(ValueError):
    pass


def unknown_keys(data: dict[str, Any], defaults: dict[str, Any] = DEFAULTS, prefix: tuple[str, ...] = ()) -> list[str]:
    """配置文件里不认识的键（多半是拼写错误），返回 “段.键” 形式的列表。"""
    found = []
    for key, value in data.items():
        path = (*prefix, key)
        if key not in defaults and path not in FREE_TABLES:
            # [segment]、[terms] 可以覆盖 [translate] 的连接设置
            if not (len(prefix) == 1 and prefix[0] in LLM_SECTIONS and key in LLM_INHERITED):
                found.append(".".join(path))
            continue
        if isinstance(value, dict) and isinstance(defaults.get(key), dict) and path not in FREE_TABLES:
            found.extend(unknown_keys(value, defaults[key], path))
    return found


def anchor_paths(data: dict[str, Any], base: Path) -> dict[str, Any]:
    """配置文件里任务目录的相对路径改为相对于 base（该配置文件所在的目录）。返回新的字典。"""
    value = data.get("workspace", {}).get("dir") if isinstance(data.get("workspace"), dict) else None
    if not isinstance(value, str) or not value:
        return data
    path = Path(value).expanduser()
    if path.is_absolute():
        return data
    return {**data, "workspace": {**data["workspace"], "dir": str(base.absolute() / path)}}


def workspace_dir(value: Any) -> str:
    """任务目录的绝对路径：留空时用数据目录，相对路径相对于当前目录。"""
    text = str(value or "").strip()
    return str((Path(text).expanduser() if text else data_dir()).resolve())


def read_config_file(path: Path) -> dict[str, Any]:
    """读取配置文件。其中的相对路径（任务目录）按配置文件所在的目录展开。"""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件 {path} 格式有误：{exc}") from exc
    return anchor_paths(data, path.parent)


def config_files(path: str | Path | None = None) -> list[Path]:
    """会被读取的配置文件（存在的）。"""
    candidates = [Path(path)] if path is not None else [user_config_path(), Path.cwd() / "yakumi.toml"]
    return [c for c in candidates if c.is_file()]


def load_config(
    path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    use_env: bool = True,
    warn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """读取配置。`warn` 用来提示配置文件里不认识的键。格式错误时抛出 ConfigError。"""
    layers = []
    for candidate in config_files(path):
        data = read_config_file(candidate)
        if warn:
            for key in unknown_keys(data):
                warn(f"配置文件 {candidate} 里有不认识的配置项 {key}，已忽略（是否拼写有误？）")
        layers.append(data)
    return build_config(layers, overrides, use_env)


def build_config(layers: list[dict[str, Any]], overrides: dict[str, Any] | None = None,
                 use_env: bool = True) -> dict[str, Any]:
    """按优先级合并：默认值 → 各配置文件的内容（按顺序）→ 环境变量 → 命令行参数。"""
    cfg = copy.deepcopy(DEFAULTS)
    for data in layers:
        merge_config_layer(cfg, data)

    if use_env:
        env_config: dict[str, dict[str, str]] = {}
        for env, (section, key) in ENV_MAP.items():
            value = os.environ.get(env)
            if value:
                env_config.setdefault(section, {})[key] = value
        merge_config_layer(cfg, env_config)

    override_config = {}
    if overrides:
        override_config = {k: {kk: vv for kk, vv in v.items() if vv is not None} for k, v in overrides.items()}
        merge_config_layer(cfg, override_config)
    if use_env:
        for section in ("asr", "translate"):
            origin = service_origin(cfg[section]["base_url"])
            env = SERVICE_KEYS.get(origin) if origin is not None else None
            if env and not cfg[section].get("api_key") and "api_key" not in override_config.get(section, {}):
                cfg[section]["api_key"] = os.environ.get(env, "")
    cfg["workspace"]["dir"] = workspace_dir(cfg["workspace"].get("dir"))
    return cfg
