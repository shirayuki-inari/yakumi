"""文件与目录：用户配置和缓存目录（遵循 XDG 约定）、JSON 的原子读写。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def config_dir() -> Path:
    """~/.config/yakumi（或 $XDG_CONFIG_HOME/yakumi）：配置文件、术语表。"""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "yakumi"


def cache_dir() -> Path:
    """~/.cache/yakumi（或 $XDG_CACHE_HOME/yakumi）：VAD 模型、识别能力探测结果。"""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "yakumi"


def write_json(path: Path, data: Any) -> None:
    """先写临时文件再改名，中途被打断也不会留下写了一半的文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
