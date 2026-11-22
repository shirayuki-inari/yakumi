"""背景资料：可保存、在多个任务里复用的翻译背景（界面和命令行里叫“背景资料”，代码和文件沿用 glossary 的名字）。

每份背景资料是 ~/.config/yakumi/glossaries/ 下的一个 TOML 文件，可以在网页或命令行（yakumi context）里管理，
也可以直接编辑：

    name = "我的频道"
    notes = \"\"\"
    VTuber 直播。主播称呼粉丝为“助手くん”。
    \"\"\"

    [terms]
    "助手くん" = "助手君"
    "ジョシュ君" = "助手君"

- notes：背景说明（核心），原样写进翻译提示词。一两句话说明这是谁的什么内容；提示词已经说明原文是
  不可靠的识别结果，模型知道背景后能自己认出被写错的名字、统一译法，通常不需要逐条列固定译法；
- terms：固定译法（可选），原文 → 译文，用于必须固定的译法。翻译时只把在原文里出现过的放进提示词。

任务可以选用多份背景资料，再加上只对这个任务有效的补充说明；配置文件里的 [translate] notes 和
[translate.glossary] 对所有任务生效。固定译法合并时后者覆盖前者：配置 → 背景资料（按选择的顺序）。
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .files import config_dir

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_NAME = 60


class GlossaryError(ValueError):
    pass


def glossaries_dir() -> Path:
    return config_dir() / "glossaries"


def glossary_id(name: str) -> str:
    """由名称得到文件名（同时作为 ID）：去掉文件名里不能用的字符。"""
    gid = _BAD_CHARS.sub("", name).strip().strip(".")
    if not gid:
        raise GlossaryError("背景资料的名称不能为空")
    return gid[:MAX_NAME]


@dataclass
class Glossary:
    name: str
    terms: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    @property
    def id(self) -> str:
        return glossary_id(self.name)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "terms": dict(self.terms), "notes": self.notes}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Glossary":
        name = str(data.get("name", "")).strip()
        terms_in = data.get("terms") or {}
        if isinstance(terms_in, list):  # 网页提交的 [{source, target}] 形式
            if not all(isinstance(t, dict) for t in terms_in):
                raise GlossaryError("固定译法的格式不正确")
            terms_in = {str(t.get("source") or ""): str(t.get("target") or "") for t in terms_in}
        elif not isinstance(terms_in, dict):
            raise GlossaryError("固定译法的格式不正确")
        terms = {}
        for src, dst in terms_in.items():
            src, dst = str(src).strip(), str(dst).strip()
            if src:
                terms[src] = dst
        glossary_id(name)  # 校验名称
        return cls(name=name, terms=terms, notes=str(data.get("notes", "")).strip())


def _toml_str(value: str) -> str:
    # JSON 的字符串转义是 TOML 基本字符串的子集
    return json.dumps(value, ensure_ascii=False)


def dumps(g: Glossary) -> str:
    lines = [f"name = {_toml_str(g.name)}"]
    if g.notes:
        if "'''" in g.notes:
            lines.append(f"notes = {_toml_str(g.notes)}")
        else:
            lines.append(f"notes = '''\n{g.notes}\n'''")
    lines += ["", "[terms]"]
    lines += [f"{_toml_str(k)} = {_toml_str(v)}" for k, v in g.terms.items()]
    return "\n".join(lines) + "\n"


def path_of(gid: str, root: Path | None = None) -> Path:
    if glossary_id(gid) != gid:
        raise GlossaryError(f"背景资料的名称不合法：{gid}")
    return (root or glossaries_dir()) / f"{gid}.toml"


def load(gid: str, root: Path | None = None) -> Glossary:
    path = path_of(gid, root)
    if not path.is_file():
        raise GlossaryError(f"找不到背景资料：{gid}（{path}）")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise GlossaryError(f"背景资料 {path} 格式有误：{exc}") from exc
    data.setdefault("name", gid)
    g = Glossary.from_dict(data)
    if g.id != gid:  # 手动改过 name 时以文件名为准
        g.name = gid
    return g


def save(g: Glossary, root: Path | None = None, old_id: str | None = None) -> Glossary:
    """保存（新建或覆盖）。改名时传入原来的 ID，会删除旧文件。"""
    root = root or glossaries_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = path_of(g.id, root)
    if old_id and old_id != g.id:
        if path.exists():
            raise GlossaryError(f"已经有名为“{g.name}”的背景资料")
        path_of(old_id, root).unlink(missing_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(dumps(g), encoding="utf-8")
    os.replace(tmp, path)
    return g


def delete(gid: str, root: Path | None = None) -> None:
    path = path_of(gid, root)
    if not path.is_file():
        raise GlossaryError(f"找不到背景资料：{gid}")
    path.unlink()


def list_all(root: Path | None = None) -> list[Glossary]:
    root = root or glossaries_dir()
    out = []
    for path in sorted(root.glob("*.toml")):
        try:
            out.append(load(path.stem, root))
        except GlossaryError:
            continue
    return out


def import_terms(text: str) -> dict[str, str]:
    """从 CSV / TSV / “原文 = 译文” 文本里读出固定译法，一行一条。"""
    terms: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("\t", "=", ",", "，"):
            if sep in line:
                src, dst = line.split(sep, 1)
                src, dst = src.strip().strip('"'), dst.strip().strip('"')
                if src:
                    terms[src] = dst
                break
    return terms


def resolve(cfg_terms: dict[str, str] | None, ids: list[str], notes: str = "",
            root: Path | None = None) -> tuple[dict[str, str], str]:
    """合并配置、选用的背景资料和任务的补充说明，得到翻译时使用的固定译法和背景说明。"""
    terms = dict(cfg_terms or {})
    sections = []
    for gid in ids:
        g = load(gid, root)
        terms.update(g.terms)
        if g.notes:
            sections.append(f"【{g.name}】\n{g.notes}" if len(ids) > 1 or notes else g.notes)
    if notes.strip():
        sections.append(f"【本视频】\n{notes.strip()}" if sections else notes.strip())
    return terms, "\n\n".join(sections)


def relevant_terms(terms: dict[str, str], texts: list[str]) -> dict[str, str]:
    """只保留在原文里出现过的固定译法（拉丁文字不区分大小写）。"""
    if not terms:
        return {}
    joined = "\n".join(texts)
    lower = joined.lower()
    return {k: v for k, v in terms.items() if k in joined or k.lower() in lower}
