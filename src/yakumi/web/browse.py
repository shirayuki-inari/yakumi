"""在网页上浏览和选择服务器上的文件。

只能访问允许的目录：任务目录（[workspace] dir，其中工作台自己的 jobs/、uploads/ 不可见），
以及配置 [web] file_roots 里列出的目录。路径先解析符号链接再检查，指向允许目录之外的链接一律拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MEDIA_SUFFIXES = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts", ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg",
    ".3gp", ".ogv", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma",
}
SUBTITLE_SUFFIXES = {".srt", ".vtt"}
KINDS = {"video": MEDIA_SUFFIXES, "subtitles": SUBTITLE_SUFFIXES}
WORKSPACE_INTERNAL = {"jobs", "uploads"}
MAX_ENTRIES = 2000


class FileAccessError(ValueError):
    pass


@dataclass(frozen=True)
class Root:
    id: int
    label: str
    path: Path
    hidden: frozenset[str] = frozenset()  # 根目录下不显示、也不允许访问的子目录

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "path": str(self.path)}


def make_roots(workspace: Path, extra: list[str]) -> list[Root]:
    roots = [Root(0, "任务目录", workspace.resolve(), frozenset(WORKSPACE_INTERNAL))]
    seen = {roots[0].path}
    for item in extra:
        path = Path(str(item)).expanduser()
        try:
            path = path.resolve(strict=True)
        except OSError:
            continue  # 不存在的目录不显示
        if path.is_dir() and path not in seen:
            seen.add(path)
            roots.append(Root(len(roots), path.name or str(path), path))
    return roots


class Browser:
    def __init__(self, roots: list[Root]):
        self.roots = roots

    def root(self, root_id: Any) -> Root:
        try:
            return self.roots[int(root_id)]
        except (ValueError, TypeError, IndexError):
            raise FileAccessError("找不到这个目录") from None

    def resolve(self, root_id: Any, rel: str) -> tuple[Root, Path]:
        """把（根目录，相对路径）解析成真实路径，确认仍在允许的范围内。"""
        root = self.root(root_id)
        rel = str(rel or "").replace("\\", "/").strip("/")
        parts = PurePosixPath(rel).parts if rel else ()
        if any(p in ("..", "") for p in parts) or (parts and PurePosixPath(rel).is_absolute()):
            raise FileAccessError("路径不正确")
        try:
            path = root.path.joinpath(*parts).resolve(strict=True)
        except (OSError, RuntimeError):
            raise FileAccessError("找不到这个文件或目录") from None
        if path != root.path and root.path not in path.parents:
            raise FileAccessError("不允许访问这个位置")
        inner = path.relative_to(root.path).parts
        if inner and inner[0] in root.hidden:
            raise FileAccessError("不允许访问这个位置")
        return root, path

    def list(self, root_id: Any, rel: str, kind: str | None = None) -> dict[str, Any]:
        root, path = self.resolve(root_id, rel)
        if not path.is_dir():
            raise FileAccessError("这不是一个目录")
        suffixes = KINDS.get(kind or "")
        entries = []
        try:
            children = list(path.iterdir())
        except OSError as exc:
            raise FileAccessError(f"无法读取这个目录：{exc.strerror or exc}") from None
        for child in children:
            name = child.name
            if name.startswith(".") or (path == root.path and name in root.hidden):
                continue
            try:
                real = child.resolve(strict=True)
                if real != root.path and root.path not in real.parents:
                    continue  # 指向允许范围之外的符号链接
                stat = real.stat()
            except (OSError, RuntimeError):
                continue
            is_dir = real.is_dir()
            if not is_dir and suffixes is not None and child.suffix.lower() not in suffixes:
                continue
            entries.append({"name": name, "dir": is_dir, "size": None if is_dir else stat.st_size,
                            "mtime": int(stat.st_mtime)})
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        truncated = len(entries) > MAX_ENTRIES
        current = path.relative_to(root.path).as_posix()
        current = "" if current == "." else current
        return {
            "roots": [r.to_dict() for r in self.roots], "root": root.id, "path": current,
            "parent": None if not current else (PurePosixPath(current).parent.as_posix() if "/" in current else ""),
            "entries": entries[:MAX_ENTRIES], "truncated": truncated,
        }

    def file(self, ref: str, kind: str) -> Path:
        """解析表单里的文件引用“根目录编号:相对路径”，确认是允许类型的文件。"""
        root_id, sep, rel = str(ref).partition(":")
        if not sep or not rel:
            raise FileAccessError("服务器文件参数不正确")
        _, path = self.resolve(root_id, rel)
        if not path.is_file():
            raise FileAccessError("选择的不是文件")
        if path.suffix.lower() not in KINDS[kind]:
            raise FileAccessError("不支持这种文件类型" if kind == "video" else "目前只能导入 SRT 或 VTT 字幕")
        return path


__all__ = ["Browser", "FileAccessError", "MEDIA_SUFFIXES", "SUBTITLE_SUFFIXES", "make_roots"]
