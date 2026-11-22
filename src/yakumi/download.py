"""视频链接下载（可选功能，需要 yt-dlp）。

yt-dlp 不是必需依赖：装了 Python 模块（pip install 'yakumi[ytdlp]'）或 PATH 里有 yt-dlp 程序时才能使用。
通过子进程调用，用 --print 和 --progress-template 输出固定格式的行来获取标题、文件路径和进度。
"""

from __future__ import annotations

import importlib.util
import ipaddress
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

Progress = Callable[[float, str], None]
MAX_LOG_BYTES = 16 * 1024 * 1024


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
        port = parts.port
    except ValueError:
        host = None
        parts = None
    if not parts or parts.scheme not in ("http", "https") or not host or len(url) > 2000:
        raise DownloadError("请输入以 http:// 或 https:// 开头的视频链接")
    if allow_private:
        return url
    try:
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        raise DownloadError(f"无法解析链接里的域名：{host}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not addr.is_global or addr.is_multicast or addr.is_reserved:
            raise DownloadError("不允许下载本机或局域网地址的链接（可以在配置 [download] allow_private 中开启）")
    return url


def download(url: str, dest: Path, cfg: dict[str, Any], progress: Progress | None = None) -> tuple[Path, str]:
    """把链接里的视频下载到 dest 目录（文件名 download.<扩展名>），返回文件路径和标题。中断后重新调用会续传。"""
    base = ytdlp_command(cfg)
    if base is None:
        raise DownloadError("没有安装 yt-dlp，无法下载视频链接。安装：pip install 'yakumi[ytdlp]'")
    url = check_url(url, bool(cfg.get("allow_private")))
    dest = dest.resolve()
    timeout = float(cfg.get("max_duration_seconds", 3600))
    max_bytes = int(cfg.get("max_size_mb", 4096)) * 1024 * 1024
    if not math.isfinite(timeout) or timeout <= 0 or max_bytes <= 0:
        raise DownloadError("下载时限和文件大小上限必须为正数")
    cmd = [
        *base, "--ignore-config", "--no-playlist", "--newline", "--progress", "--no-simulate", "--no-colors",
        "--socket-timeout", "30", "--max-filesize", str(max_bytes),
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

    def process_line(line: str) -> None:
        nonlocal title, filepath
        if line.startswith("YAKUMI-TITLE "):
            title = line[len("YAKUMI-TITLE "):].strip()
        elif line.startswith("YAKUMI-FILE "):
            filepath = line[len("YAKUMI-FILE "):].strip()
        elif line.startswith("YAKUMI-PROGRESS "):
            if progress:
                progress(*_parse_progress(line))
        elif line.strip():
            tail.append(line[-2000:])

    def check_size() -> None:
        files = (p for p in dest.glob("download.*") if not p.is_symlink() and p.is_file())
        sizes = 0
        for path in files:
            try:
                sizes += path.stat().st_size
            except FileNotFoundError:
                continue  # yt-dlp 可在检查时把临时文件原子改名
        if sizes > max_bytes:
            raise DownloadError(f"下载文件超过大小上限（{max_bytes // 1048576} MB）")

    # 日志由独立的文件句柄读取。子进程无输出时也能检查截止时间与磁盘用量。
    with tempfile.TemporaryDirectory(prefix="yakumi-download-") as tmp:
        log_path = Path(tmp) / "output.log"
        with log_path.open("wb") as output, log_path.open("rb") as log:
            try:
                proc = subprocess.Popen(cmd, stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        start_new_session=os.name != "nt")
            except OSError as exc:
                raise DownloadError(f"无法运行 yt-dlp：{exc}") from exc
            deadline = time.monotonic() + timeout
            pending = b""
            try:
                while True:
                    if time.monotonic() >= deadline:
                        raise DownloadError(f"下载超过时限（{timeout:g} 秒）")
                    if log_path.stat().st_size > MAX_LOG_BYTES:
                        raise DownloadError("下载程序输出的日志过多")
                    check_size()
                    code = proc.poll()
                    chunk = log.read(65536)
                    if chunk:
                        *lines, pending = (pending + chunk).split(b"\n")
                        for raw in lines:
                            process_line(raw.decode("utf-8", "replace"))
                    elif code is not None:
                        if pending:
                            process_line(pending.decode("utf-8", "replace"))
                        break
                    else:
                        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
            except BaseException:
                _stop_download(proc)
                raise
    if code != 0:
        errors = [ln for ln in tail if ln.startswith("ERROR")] or list(tail)[-3:]
        raise DownloadError("下载失败：" + ("\n".join(errors) or f"yt-dlp 退出码 {code}"))
    def is_output(path: Path) -> bool:
        return (not path.is_symlink() and path.resolve().parent == dest and path.stem == "download"
                and path.suffix not in ("", ".part", ".ytdl") and path.is_file())

    if filepath:
        path = Path(filepath)
        if not is_output(path):
            raise DownloadError("下载程序返回的文件不在当前任务的输出位置")
    else:
        found = [p for p in dest.glob("download.*") if is_output(p)]
        if not found:
            raise DownloadError("下载完成，但没有找到下载的文件")
        path = max(found, key=lambda p: p.stat().st_size)
    return path, title


def _stop_download(proc: subprocess.Popen) -> None:
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif proc.poll() is None:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError):
            proc.kill()
    proc.wait()


def _parse_progress(line: str) -> tuple[float, str]:
    parts = line.split()
    try:
        done = float(parts[1])
        total = float(parts[2])
    except (IndexError, ValueError):
        return 0.0, "下载视频"
    if not math.isfinite(done) or not math.isfinite(total) or done < 0:
        return 0.0, "下载视频"
    if total <= 0:
        return 0.0, f"下载视频 {done / 1048576:.1f} MB"
    return min(1.0, done / total), f"下载视频 {done / 1048576:.1f}/{total / 1048576:.1f} MB"


__all__ = ["DownloadError", "available", "check_url", "download", "ytdlp_command"]
