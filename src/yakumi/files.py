"""文件与目录：用户配置和缓存目录（遵循 XDG 约定）、JSON 的原子读写。"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from errno import EACCES, EAGAIN, EDEADLK
from pathlib import Path
from typing import Any, Iterator


def config_dir() -> Path:
    """~/.config/yakumi（或 $XDG_CONFIG_HOME/yakumi）：配置文件、背景资料。"""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "yakumi"


def cache_dir() -> Path:
    """~/.cache/yakumi（或 $XDG_CACHE_HOME/yakumi）：VAD 模型、识别能力探测结果。"""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "yakumi"


def write_json(path: Path, data: Any) -> None:
    """先写临时文件再改名，中途被打断也不会留下写了一半的文件。"""
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    f = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    tmp = Path(f.name)
    try:
        with f:
            f.write(payload)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class FileBusyError(RuntimeError):
    pass


@contextmanager
def lock_file(path: Path) -> Iterator[None]:
    """非阻塞文件锁，句柄关闭时由操作系统释放。锁文件需保留供其他进程使用。"""
    with path.open("a+b") as f:
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (EACCES, EAGAIN, EDEADLK):
                raise
            raise FileBusyError from exc
        yield


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
