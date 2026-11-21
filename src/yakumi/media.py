"""ffmpeg / ffprobe 封装。"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


class MediaError(RuntimeError):
    pass


def _bin(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaError(f"找不到 {name}，请先安装 ffmpeg")
    return path


def _run(args: list[str], cwd: str | Path | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise MediaError(f"{Path(args[0]).name} 执行失败：\n{tail}")
    return proc


def probe_duration(path: str | Path) -> float:
    proc = _run(
        [_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]
    )
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (KeyError, ValueError, TypeError) as exc:
        raise MediaError(f"无法读取时长：{path}") from exc


def has_audio(path: str | Path) -> bool:
    proc = _run(
        [_bin("ffprobe"), "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)]
    )
    return bool(proc.stdout.strip())


CODECS = {
    ".wav": ["-c:a", "pcm_s16le"],
    # 必须显式指定 16 位：从 mp3 等浮点解码器转出的 FLAC 默认是 24 位，体积翻倍且解码极慢
    ".flac": ["-sample_fmt", "s16", "-c:a", "flac"],
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "48k"],
    # Opus 32kbps（语音优化），约 0.21MB/分钟，适合上行带宽有限时上传
    ".ogg": ["-c:a", "libopus", "-b:a", "32k", "-application", "voip"],
}
UPLOAD_FORMATS = {"flac": ".flac", "opus": ".ogg"}
# MP4 能直接容纳、烧录时不需要重新编码的音频
MP4_AUDIO = {"aac", "mp3", "alac", "ac3", "eac3", "opus", "flac"}


def upload_suffix(fmt: str) -> str:
    """配置 asr.upload_format → 上传片段的扩展名。"""
    try:
        return UPLOAD_FORMATS[fmt]
    except KeyError:
        raise MediaError(f"未知的上传格式：{fmt}（可选 {' / '.join(UPLOAD_FORMATS)}）") from None


def audio_codec(path: str | Path) -> str:
    """第一条音轨的编码名（例如 aac、opus、vorbis），没有音轨时返回空字符串。"""
    proc = _run([_bin("ffprobe"), "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                 "-of", "csv=p=0", str(path)])
    return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""


def extract_audio(video: str | Path, out: str | Path) -> Path:
    """抽取 16kHz 单声道 16 位音频，编码由扩展名决定（.wav / .flac / .mp3）。"""
    if not has_audio(video):
        raise MediaError("视频里没有音轨")
    out = Path(out)
    codec = CODECS.get(out.suffix.lower(), CODECS[".wav"])
    _run([_bin("ffmpeg"), "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
          *codec, str(out)])
    return out


def extract_wav(src: str | Path, out: str | Path) -> Path:
    """whisper.cpp 需要 16kHz 单声道 WAV。"""
    out = Path(out)
    _run([_bin("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
          "-c:a", "pcm_s16le", str(out)])
    return out


def extract_clip(src: str | Path, start: float, end: float, out: str | Path) -> Path:
    """截取 [start, end) 区间，编码为 16kHz 单声道音频，编码由扩展名决定。

    默认 .flac（16 位无损，约 0.8MB/分钟）；.ogg 为 Opus 32kbps（约 0.21MB/分钟）。
    源文件是 FLAC 时，中途 seek 后第一帧的采样数不固定，FLAC 编码器会报错（invalid block size），
    所以用 asetnsamples 把帧重新打包成固定大小。seek 本身是采样级精确的。
    """
    out = Path(out)
    codec = CODECS.get(out.suffix.lower(), CODECS[".flac"])
    _run([_bin("ffmpeg"), "-y", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{end - start:.3f}",
          "-i", str(src), "-vn", "-af", "asetnsamples=n=4096:p=0", "-ac", "1", "-ar", "16000",
          *codec, str(out)])
    return out


_FILTER_SPECIAL = set(":'\\[],;=")


def _style(font: str, font_size: int) -> str:
    parts = [f"FontSize={font_size}", "Outline=1", "Shadow=0", "MarginV=24"]
    if font and not (_FILTER_SPECIAL & set(font)):
        parts.insert(0, f"FontName={font}")
    return ",".join(parts)


def burn_subtitles(video: str | Path, subtitles: str | Path, out: str | Path,
                   font: str = "", font_size: int = 18, fonts_dir: str = "") -> Path:
    """把字幕烧录进画面。

    滤镜参数里的路径需要多层转义，这里把字幕复制到临时目录并以固定文件名引用，避开转义问题。
    `font` 是字体名（交给 fontconfig 查找），`fonts_dir` 是额外的字体目录。
    """
    video, subtitles, out = Path(video).resolve(), Path(subtitles), Path(out).resolve()
    with tempfile.TemporaryDirectory(prefix="yakumi-burn-") as tmp:
        name = "subs" + subtitles.suffix.lower()
        shutil.copyfile(subtitles, Path(tmp) / name)
        vf = f"subtitles={name}"
        fonts = str(Path(fonts_dir).expanduser()) if fonts_dir else ""
        if fonts and Path(fonts).is_dir() and not (_FILTER_SPECIAL & set(fonts)):
            vf += f":fontsdir={fonts}"
        if subtitles.suffix.lower() != ".ass":
            vf += f":force_style='{_style(font, font_size)}'"
        # WebM 常见的 Vorbis 音频放不进 MP4，这类情况转成 AAC，其余直接复制
        audio = ["-c:a", "copy"] if audio_codec(video) in MP4_AUDIO else ["-c:a", "aac", "-b:a", "192k"]
        _run([_bin("ffmpeg"), "-y", "-v", "error", "-i", str(video), "-vf", vf,
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", *audio, str(out)], cwd=tmp)
    return out


def ffmpeg_has_libass() -> bool:
    try:
        proc = _run([_bin("ffmpeg"), "-hide_banner", "-filters"])
    except MediaError:
        return False
    return any(line.split()[1:2] == ["subtitles"] for line in proc.stdout.splitlines() if line.strip())
