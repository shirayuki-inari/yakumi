"""候选关联：去重、把同一实体的不同称呼并在一起、优先对应到背景资料里已有的实体。

- 原文片段本身就是已有实体的规范名称或别名 → 对应到这个实体（不依赖模型给出的规范名称）；
- 否则模型给出的规范名称是已有实体的名称或别名 → 对应到这个实体；
- 否则按模型给出的规范名称（没有时按片段本身）归为一个新实体，所以没有规范名称的片段会并入以它为规范名称的那一组；
- 每个片段在全部句子里逐字查找，补全模型没有逐一列出的出现位置；被更长的片段包含的出现位置不重复计算。

只由已确认的名称、别名逐字匹配到的实体，审核时直接标为已通过（它们本来就在背景资料里）。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..glossary import Entry, find_surface, norm_text
from .review import ReviewEntity, ReviewOccurrence
from .validator import CONFIDENCE, Candidate, SentenceText


def _most_common(values: list[str | None]) -> str | None:
    values = [v for v in values if v]
    return Counter(values).most_common(1)[0][0] if values else None


def resolve(candidates: list[Candidate], sentences: list[SentenceText], library: list[Entry],
            target: str | None) -> list[ReviewEntity]:
    index: dict[str, Entry] = {}
    for e in library:
        for s in e.surfaces():
            index.setdefault(norm_text(s), e)

    groups: dict[str, dict[str, Any]] = {}

    def group(key: str, entry: Entry | None) -> dict[str, Any]:
        return groups.setdefault(key, {"entry": entry, "cands": [], "exact": True})

    for c in candidates:
        entry = index.get(norm_text(c.surface))
        exact = entry is not None
        if entry is None and c.canonical:
            entry = index.get(norm_text(c.canonical))
        if entry is not None:
            g = group(f"lib:{entry.id}", entry)
        else:
            g = group(f"new:{norm_text(c.canonical or c.surface)}", None)
        g["cands"].append(c)
        g["exact"] = g["exact"] and exact

    # 逐字查找每个片段的全部出现位置；同一句里被更长片段覆盖的位置不算
    spans: list[tuple[int, int, int, str, str]] = []  # (句子, 起点, 终点, 组, 片段)
    for key, g in groups.items():
        for surface in dict.fromkeys(c.surface for c in g["cands"]):
            for s in sentences:
                pos = find_surface(surface, s.text)
                while pos >= 0:
                    spans.append((s.id, pos, pos + len(surface), key, s.text[pos:pos + len(surface)]))
                    pos = find_surface(surface, s.text, pos + 1)
    kept: dict[str, set[tuple[int, int, str]]] = {}
    by_sentence: dict[int, list[tuple[int, int, int, str, str]]] = {}
    for span in spans:
        by_sentence.setdefault(span[0], []).append(span)
    for sid, items in by_sentence.items():
        for sp in items:
            covered = any(o is not sp and o[1] <= sp[1] and sp[2] <= o[2] and (o[2] - o[1]) > (sp[2] - sp[1])
                          for o in items)
            if not covered:
                kept.setdefault(sp[3], set()).add((sid, sp[1], sp[4]))

    times = {s.id: s.start for s in sentences}
    entities = []
    for key, g in groups.items():
        cands: list[Candidate] = g["cands"]
        entry: Entry | None = g["entry"]
        model_sites = {(c.sentence_id, c.surface) for c in cands}
        occurrences = [ReviewOccurrence(sid, surface, times.get(sid, 0.0),
                                        "model" if (sid, surface) in model_sites else "search", offset=pos)
                       for sid, pos, surface in sorted(kept.get(key, ()), key=lambda x: (times.get(x[0], 0.0), *x))]
        if not occurrences:
            continue
        lib_tr = entry.translation_for(target) if entry else None
        auto = entry is not None and g["exact"]
        entities.append(ReviewEntity(
            key=key,
            status="approved" if auto else "pending",
            canonical=entry.canonical if entry else _most_common([c.canonical for c in cands]),
            translation=lib_tr if lib_tr is not None else _most_common([c.translation for c in cands]),
            category=(entry.category if entry and entry.category else _most_common([c.category for c in cands]))
            or "other",
            confidence=min((c.confidence for c in cands), key=CONFIDENCE.index),
            library_id=entry.id if entry else None,
            auto=auto,
            surfaces=list(dict.fromkeys(c.surface for c in cands)),
            evidence=list(dict.fromkeys(c.evidence for c in cands if c.evidence))[:5],
            occurrences=occurrences,
            translation_from_library=lib_tr is not None,
        ))
    entities.sort(key=lambda e: (e.occurrences[0].start, e.key))
    return entities


__all__ = ["resolve"]
