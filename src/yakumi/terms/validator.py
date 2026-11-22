"""校验模型返回的术语候选：结构是否完整、句子编号是否存在、原文片段是否真的在那一句里。

原文片段（surface）必须是对应句子里真实存在的连续片段。校验顺序：
1. 在指定的句子里逐字查找；
2. 忽略全角半角、大小写和空白后查找，找到时换成原文里的实际写法；
3. 在同一块的其他句子里查找（模型写错了编号），取离指定编号最近的一句；
仍然找不到时记为无效，交给扫描器做一次定向修复（见 scanner.py）。
"""

from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

from ..glossary import CATEGORIES

CONFIDENCE = ("high", "medium", "low")
MAX_SURFACE = 40


@dataclass
class SentenceText:
    id: int
    text: str
    start: float = 0.0
    end: float = 0.0


@dataclass
class Candidate:
    sentence_id: int
    surface: str
    canonical: str | None
    translation: str | None
    category: str
    confidence: str
    evidence: str = ""
    fixed: str = ""  # 校验时做过的修正：normalized / moved / repaired

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate":
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__ if k in data})


def _opt_str(value: Any, limit: int = 100) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.lower() in ("null", "none", "unknown", "n/a") or len(value) > limit:
        return None
    return value


def locate(surface: str, text: str) -> str | None:
    """surface 在 text 里的实际写法：逐字找到时原样返回；忽略全角半角、大小写和空白后找到时，返回原文里对应的那一段。"""
    if not surface:
        return None
    if surface in text:
        return surface
    norm_chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).lower():
            if not c.isspace():
                norm_chars.append(c)
                origin.append(i)
    key = "".join(c for c in unicodedata.normalize("NFKC", surface).lower() if not c.isspace())
    if not key:
        return None
    pos = "".join(norm_chars).find(key)
    if pos < 0:
        return None
    return text[origin[pos]:origin[pos + len(key) - 1] + 1]


def validate_item(item: Any, sentences: dict[int, SentenceText], chunk_ids: list[int]) -> tuple[Candidate | None, str]:
    """校验一项候选。返回（候选，空字符串）或（None，无效原因）。"""
    if not isinstance(item, dict):
        return None, "不是对象"
    surface = item.get("surface")
    if not isinstance(surface, str) or not surface.strip():
        return None, "缺少原文片段"
    surface = surface.strip()
    if len(surface) > MAX_SURFACE:
        return None, "原文片段太长"
    try:
        sid = int(item.get("sentence_id"))
    except (TypeError, ValueError):
        sid = None
    category = str(item.get("category") or "other").strip().lower()
    confidence = str(item.get("confidence") or "low").strip().lower()
    cand = Candidate(
        sentence_id=sid if sid is not None else -1, surface=surface,
        canonical=_opt_str(item.get("canonical")), translation=_opt_str(item.get("translation")),
        category=category if category in CATEGORIES else "other",
        confidence=confidence if confidence in CONFIDENCE else "low",
        evidence=(_opt_str(item.get("evidence"), 300) or ""),
    )
    if sid in sentences:
        found = locate(surface, sentences[sid].text)
        if found is not None:
            if found != surface:
                cand.surface, cand.fixed = found, "normalized"
            return cand, ""
    # 编号不对或不存在：在同一块里找，取离指定编号最近的一句
    order = sorted(chunk_ids, key=lambda i: abs(i - sid) if sid is not None else 0)
    for other in order:
        if other in sentences:
            found = locate(surface, sentences[other].text)
            if found is not None:
                cand.sentence_id, cand.surface, cand.fixed = other, found, "moved"
                return cand, ""
    return None, "原文里找不到这个片段" if sid in sentences else "句子编号不存在"


__all__ = ["CONFIDENCE", "Candidate", "SentenceText", "locate", "validate_item"]
