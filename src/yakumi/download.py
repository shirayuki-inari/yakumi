"""视频链接下载（可选功能，需要 yt-dlp）。

yt-dlp 不是必需依赖：装了 Python 模块（pip install 'yakumi[ytdlp]'）或 PATH 里有 yt-dlp 程序时才能使用。
通过子进程调用，用 --print 和 --progress-template 输出固定格式的行来获取标题、文件路径和进度。
"""

from __future__ import annotations

import importlib.util
import ipaddress
import shutil
import socket
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

Progress = Callable[[float, str], None]


class DownloadError(RuntimeError):
    pass


def ytdlp_command(cfg: dict[str, Any]) -> list[str] | None:
    """调用 yt-dlp 的命令；没有安装时返回 None。"""
    custom = str(cfg.get("ytdlp") or "").strip()
    if custom:
        path = Path(custom).expanduser()
        found = str(path) if path.is_file() else shutil.which(custom)
        return [found] if found else None
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    exe = shutil.which("yt-dlp")
    return [exe] if exe else None


def available(cfg: dict[str, Any]) -> bool:
    return ytdlp_command(cfg) is not None


def check_url(url: str, allow_private: bool = False) -> str:
    """只接受 http(s) 链接。默认拒绝指向本机、局域网等内部地址的链接，避免借工作台访问内网服务。"""
    url = url.strip()
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        host = None
        parts = None
    if not parts or parts.scheme not in ("http", "https") or not host or len(url) > 2000:
        raise DownloadError("请输入以 http:// 或 https:// 开头的视频链接")
    if allow_private:
        return url
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        raise DownloadError(f"无法解析链接里的域名：{host}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not addr.is_global:
            raise DownloadError("不允许下载本机或局域网地址的链接（可以在配置 [download] allow_private 中开启）")
    return url


def download(url: str, dest: Path, cfg: dict[str, Any], progress: Progress | None = None) -> tuple[Path, str]:
    """把链接里的视频下载到 dest 目录（文件名 download.<扩展名>），返回文件路径和标题。中断后重新调用会续传。"""
    base = ytdlp_command(cfg)
    if base is None:
        raise DownloadError("没有安装 yt-dlp，无法下载视频链接。安装：pip install 'yakumi[ytdlp]'")
    cmd = [
        *base, "--no-playlist", "--newline", "--progress", "--no-simulate", "--no-colors",
        "-f", str(cfg.get("format") or "bv*+ba/b"), "--merge-output-format", "mp4",
        "-o", str(dest / "download.%(ext)s"),
        "--print", "before_dl:YAKUMI-TITLE %(title)s",
        "--print", "after_move:YAKUMI-FILE %(filepath)s",
        "--progress-template",
        "download:YAKUMI-PROGRESS %(progress.downloaded_bytes)s %(progress.total_bytes,progress.total_bytes_estimate)s",
        *[str(a) for a in cfg.get("extra_args") or []],
        "--", url,
    ]
    title, filepath = "", ""
    tail: deque[str] = deque(maxlen=20)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DownloadError(f"无法运行 yt-dlp：{exc}") from exc
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if line.startswith("YAKUMI-TITLE "):
                title = line[len("YAKUMI-TITLE "):].strip()
            elif line.startswith("YAKUMI-FILE "):
                filepath = line[len("YAKUMI-FILE "):].strip()
            elif line.startswith("YAKUMI-PROGRESS "):
                if progress:
                    progress(*_parse_progress(line))
            elif line.strip():
                tail.append(line)
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if code != 0:
        errors = [ln for ln in tail if ln.startswith("ERROR")] or list(tail)[-3:]
        raise DownloadError("下载失败：" + ("\n".join(errors) or f"yt-dlp 退出码 {code}"))
    path = Path(filepath) if filepath else None
    if path is None or not path.is_file():
        found = [p for p in dest.glob("download.*") if p.suffix not in (".part", ".ytdl") and p.is_file()]
        if not found:
            raise DownloadError("下载完成，但没有找到下载的文件")
        path = max(found, key=lambda p: p.stat().st_size)
    return path, title


def _parse_progress(line: str) -> tuple[float, str]:
    parts = line.split()
    try:
        done = float(parts[1])
        total = float(parts[2])
    except (IndexError, ValueError):
        return 0.0, "下载视频"
    if total <= 0:
        return 0.0, f"下载视频 {done / 1048576:.1f} MB"
    return min(1.0, done / total), f"下载视频 {done / 1048576:.1f}/{total / 1048576:.1f} MB"


__all__ = ["DownloadError", "available", "check_url", "download", "ytdlp_command"]
