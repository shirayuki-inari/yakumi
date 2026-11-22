"""术语审核：审核数据、状态持久化，以及审核结果如何用于翻译、如何保存到背景资料。网页和命令行共用。

任务目录下的三个文件：

    term_candidates.json  扫描得到的候选（已校验）和无效记录，只作记录
    term_review.json      审核数据：实体（建议的规范名称、译名、状态）及其在原文里的出现位置
    glossary.json         翻译时实际使用的任务级术语表（翻译阶段生成）

区分两种信息：实体定义（规范名称、确认的别名、译名）和出现记录（句子编号、原文片段、审核状态）。
例如把“博士小寄り”关联到“博衣こより”，只记录这句话里的这个片段指向哪个实体，不改动字幕原文，
也不会把识别错误的写法加入背景资料的别名。

实体状态：pending（待审核）、approved（通过）、rejected（拒绝）。出现记录的状态默认跟随实体，
也可以单独拒绝某一处（rejected）。审核结果默认只对这个任务有效，用户可以把通过的实体保存到背景资料。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import glossary as glossary_store
from ..files import read_json, write_json
from ..glossary import CATEGORIES, Entry, GlossaryError, norm_lang, norm_text

REVIEW_FILE = "term_review.json"
CANDIDATES_FILE = "term_candidates.json"
STATUSES = ("pending", "approved", "rejected")


class ReviewError(ValueError):
    pass


@dataclass
class ReviewOccurrence:
    sentence_id: int
    surface: str
    start: float = 0.0
    found_by: str = "model"  # model：模型指出的位置；search：按片段逐字查找补全的位置
    status: str = "pending"  # pending：跟随实体；rejected：这一处不适用
    offset: int | None = None  # 在句子里的字符偏移。旧审核数据没有偏移。

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewOccurrence":
        return cls(int(data["sentence_id"]), str(data["surface"]), float(data.get("start", 0.0)),
                   str(data.get("found_by", "model")), str(data.get("status", "pending")), data.get("offset"))

    def location(self) -> tuple[int, str, int | None]:
        return self.sentence_id, self.surface, self.offset


@dataclass
class ReviewEntity:
    key: str
    status: str
    canonical: str | None
    translation: str | None
    category: str
    confidence: str
    library_id: str | None  # 对应的背景资料实体
    auto: bool  # 由程序判定（与背景资料逐字对应，或自动审核），而不是人工确认
    surfaces: list[str]
    evidence: list[str]
    occurrences: list[ReviewOccurrence]
    aliases: list[str] = field(default_factory=list)  # 用户确认、保存到背景资料时一并保存的别名
    edited: bool = False
    saved_to: list[str] = field(default_factory=list)
    merged_keys: list[str] = field(default_factory=list)
    overrides: list[str] | None = None  # None 表示旧数据，只有 edited 可用于判断
    translation_from_library: bool | None = None
    status_overridden: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewEntity":
        fields = {k: data[k] for k in cls.__dataclass_fields__ if k in data and k != "occurrences"}
        return cls(**fields, occurrences=[ReviewOccurrence.from_dict(o) for o in data.get("occurrences", [])])

    @property
    def name(self) -> str:
        return self.canonical or (self.surfaces[0] if self.surfaces else self.key)


@dataclass
class Review:
    target: str
    entities: list[ReviewEntity] = field(default_factory=list)
    finished: bool = False
    updated_at: float = 0.0
    archived: list[ReviewEntity] = field(default_factory=list)

    def find(self, key: str) -> ReviewEntity:
        for e in self.entities:
            if e.key == key:
                return e
        raise ReviewError(f"找不到术语：{key}")

    def by_number(self, ref: str) -> ReviewEntity:
        """命令行用：按列表里的序号（从 1 开始）或 key 查找。"""
        if ref.isdigit() and 1 <= int(ref) <= len(self.entities):
            return self.entities[int(ref) - 1]
        return self.find(ref)

    def summary(self) -> dict[str, int]:
        counts = {s: 0 for s in STATUSES}
        for e in self.entities:
            counts[e.status] = counts.get(e.status, 0) + 1
        return {"total": len(self.entities), **counts}

    def to_dict(self) -> dict[str, Any]:
        return {"target": self.target, "finished": self.finished, "updated_at": self.updated_at,
                "entities": [e.to_dict() for e in self.entities], "archived": [e.to_dict() for e in self.archived]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Review":
        return cls(target=str(data.get("target", "")), finished=bool(data.get("finished")),
                   updated_at=float(data.get("updated_at", 0.0)),
                   entities=[ReviewEntity.from_dict(e) for e in data.get("entities", [])],
                   archived=[ReviewEntity.from_dict(e) for e in data.get("archived", [])])


def load_review(job_dir: Path) -> Review | None:
    path = job_dir / REVIEW_FILE
    return Review.from_dict(read_json(path)) if path.is_file() else None


def save_review(job_dir: Path, review: Review) -> None:
    review.updated_at = time.time()
    write_json(job_dir / REVIEW_FILE, review.to_dict())


# ---- 修改 -----------------------------------------------------------------
def _opt_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or "\n" in value or len(value) > 200:
        raise ReviewError(f"{label}的格式不正确")
    return value.strip() or None


def apply_update(review: Review, update: Any) -> ReviewEntity:
    """修改一个实体：status、canonical、translation、category、aliases，以及单独拒绝某些出现位置。"""
    if not isinstance(update, dict) or not isinstance(update.get("key"), str):
        raise ReviewError("审核数据的格式不正确")
    e = review.find(update["key"])
    changed = False
    if "status" in update:
        if update["status"] not in STATUSES:
            raise ReviewError(f"未知的审核状态：{update['status']}")
        changed |= e.status != update["status"]
        e.status_overridden |= e.status != update["status"]
        e.status = update["status"]
    overrides = set(_overrides(e))
    for key, label in (("canonical", "规范名称"), ("translation", "译名")):
        if key in update:
            value = _opt_text(update[key], label)
            if value != getattr(e, key):
                setattr(e, key, value)
                changed = e.edited = True
                overrides.add(key)
                if key == "translation":
                    e.translation_from_library = False
    if "category" in update:
        if not isinstance(update["category"], str) or update["category"] not in CATEGORIES:
            raise ReviewError(f"未知的类别：{update['category']}")
        if e.category != update["category"]:
            e.category = update["category"]
            changed = e.edited = True
            overrides.add("category")
    if "aliases" in update:
        aliases = update["aliases"]
        if isinstance(aliases, str):
            aliases = [a for a in (x.strip() for x in aliases.replace("，", "、").replace(",", "、").split("、")) if a]
        if not isinstance(aliases, list) or not all(isinstance(a, str) and len(a) <= 200 and "\n" not in a
                                                    for a in aliases):
            raise ReviewError("别名的格式不正确")
        e.aliases = [a.strip() for a in dict.fromkeys(aliases) if a.strip()]
        overrides.add("aliases")
        changed = True
    if "occurrences" in update:
        items = update["occurrences"]
        if not isinstance(items, list):
            raise ReviewError("出现位置的格式不正确")
        for item in items:
            if not isinstance(item, dict) or item.get("status") not in ("pending", "rejected"):
                raise ReviewError("出现位置的格式不正确")
            matches = [o for o in e.occurrences if o.sentence_id == item.get("sentence_id")
                       and o.surface == item.get("surface") and ("offset" not in item or o.offset == item["offset"])]
            if len(matches) != 1:
                raise ReviewError("找不到这个出现位置")
            matches[0].status = item["status"]
            changed = True
    if changed:
        e.auto = False
        e.overrides = sorted(overrides)
    return e


def merge(review: Review, target_key: str, source_keys: list[str]) -> ReviewEntity:
    """把几个实体并成一个（例如没能自动关联的同一人物的不同称呼），出现位置全部保留。"""
    target = review.find(target_key)
    for key in dict.fromkeys(source_keys):
        src = review.find(key)
        if src is target:
            continue
        sites = {o.location(): o for o in target.occurrences}
        for occurrence in src.occurrences:
            if occurrence.location() not in sites:
                target.occurrences.append(occurrence)
            elif occurrence.status == "rejected":
                sites[occurrence.location()].status = "rejected"
        target.surfaces = list(dict.fromkeys(target.surfaces + src.surfaces))
        target.aliases = list(dict.fromkeys(target.aliases + src.aliases))
        target.evidence = list(dict.fromkeys(target.evidence + src.evidence))[:5]
        target.saved_to = list(dict.fromkeys(target.saved_to + src.saved_to))
        target.canonical = target.canonical or src.canonical
        if target.translation is None:
            target.translation, target.translation_from_library = src.translation, src.translation_from_library
        target.library_id = target.library_id or src.library_id
        target.merged_keys = list(dict.fromkeys([*target.merged_keys, src.key, *src.merged_keys]))
        review.entities.remove(src)
    target.occurrences.sort(key=lambda o: (o.start, o.sentence_id, o.offset if o.offset is not None else -1))
    target.auto, target.edited = False, True
    target.overrides = sorted(set(_overrides(target)) | {"canonical", "translation", "category", "aliases"})
    return target


def auto_approve(review: Review) -> int:
    """不经人工审核时：只采用对应到背景资料已有实体、且置信度不低的候选；新发现的实体不采用。"""
    n = 0
    for e in review.entities:
        if e.status == "pending" and e.library_id and e.confidence != "low":
            e.status, e.auto = "approved", True
            n += 1
    review.finished = True
    return n


def _overrides(e: ReviewEntity) -> list[str]:
    if e.overrides is not None:
        return e.overrides
    return ["canonical", "translation", "category", "aliases"] if e.edited else (["aliases"] if e.aliases else [])


def carry_over(new: Review, old: Review | None) -> None:
    """重新扫描时保留已有的审核决定（按实体的 key 对应；句子编号相同的出现位置保留单独拒绝的状态）。"""
    if old is None:
        return
    previous = {e.key: e for e in [*old.archived, *old.entities]}
    for before in previous.values():
        current = {e.key: e for e in new.entities}
        members = [key for key in (before.key, *before.merged_keys) if key in current]
        # 旧版合并没有记录被并入的 key。仅从能对应到旧称呼及旧位置的条目恢复。
        if before.overrides is None and before.edited and not before.merged_keys:
            surfaces = {norm_text(s) for s in before.surfaces}
            locations = {(o.sentence_id, o.surface) for o in before.occurrences}
            members.extend(e.key for e in new.entities if e.key not in members and e.key not in previous
                           and {norm_text(s) for s in e.surfaces} <= surfaces
                           and any((o.sentence_id, o.surface) in locations for o in e.occurrences))
        if members and (before.merged_keys or members != [before.key]):
            combined = merge(new, members[0], members[1:])
            combined.key = before.key
            combined.merged_keys = list(dict.fromkeys([*before.merged_keys, *(k for k in members if k != before.key)]))
            combined.library_id = before.library_id or combined.library_id
    for e in new.entities:
        before = previous.get(e.key)
        if before is None or before.auto:
            continue
        overrides = _overrides(before)
        rejected = {o.location() for o in before.occurrences if o.status == "rejected"}
        if before.status == "pending" and not (before.status_overridden or overrides or rejected or before.merged_keys):
            continue
        for key in overrides:
            if key != "translation" or not _inherits_translation(before):
                setattr(e, key, getattr(before, key))
        if "translation" in overrides and not _inherits_translation(before):
            e.translation_from_library = False
        e.status, e.edited, e.auto = before.status, before.edited, False
        e.status_overridden = before.status_overridden
        e.overrides = list(overrides)
        e.saved_to = before.saved_to
        for o in e.occurrences:
            if o.location() in rejected or (o.sentence_id, o.surface, None) in rejected:
                o.status = "rejected"
    active = {e.key for e in new.entities}
    new.archived = [e for e in previous.values() if e.key not in active and not e.auto
                    and (e.status != "pending" or e.status_overridden or e.merged_keys or _overrides(e)
                         or any(o.status == "rejected" for o in e.occurrences))]


# ---- 用于翻译 ---------------------------------------------------------------
def entity_id(e: ReviewEntity) -> str:
    return e.library_id or f"job:{e.key}"


def _inherits_translation(e: ReviewEntity) -> bool:
    return e.translation_from_library if e.translation_from_library is not None else bool(e.auto and e.library_id)


def approved_terms(review: Review | None) -> tuple[list[Entry], list[dict[str, Any]]]:
    """通过的实体（任务级实体定义）和它们的出现记录，供翻译使用。"""
    if review is None:
        return [], []
    entries: list[Entry] = []
    occurrences: list[dict[str, Any]] = []
    lang = norm_lang(review.target)
    for e in review.entities:
        if e.status != "approved" and (e.status != "rejected" or not e.library_id):
            continue
        eid = entity_id(e)
        if e.status == "approved":
            translated = {} if e.translation is None or _inherits_translation(e) else {lang: e.translation}
            entries.append(Entry(canonical=e.name, id=eid, category=e.category, aliases=list(e.aliases), translations=translated))
        for o in e.occurrences:
            site = {"sentence_id": o.sentence_id, "surface": o.surface, "entity": eid}
            if o.offset is not None:
                site["offset"] = o.offset
            if e.status == "rejected" or o.status == "rejected":
                site["rejected"] = True
            occurrences.append(site)
    return entries, occurrences


def save_to_library(e: ReviewEntity, gid: str, target: str) -> Entry:
    """把一个通过的实体保存到背景资料：规范名称、用户确认的别名、这种目标语言的译名。识别错误的写法不会保存。"""
    _check_export(e)
    try:
        library = glossary_store.load(gid)
        saved = add_to_library(e, library, target)
        glossary_store.save(library)
    except GlossaryError as exc:
        raise ReviewError(str(exc)) from exc
    if gid not in e.saved_to:
        e.saved_to.append(gid)
    return saved


def _check_export(e: ReviewEntity) -> None:
    if e.status != "approved":
        raise ReviewError(f"“{e.name}”还没有通过审核，不能保存到背景资料")
    if not e.canonical:
        raise ReviewError(f"“{e.name}”没有规范名称，请先填写")


def add_to_library(e: ReviewEntity, library: glossary_store.Glossary, target: str) -> Entry:
    """在内存中准备导出条目，以便整批校验后一起保存。"""
    _check_export(e)
    existing = next((x for x in library.entries if e.library_id and x.id == e.library_id), None)
    entry = Entry(canonical=e.canonical, aliases=list(e.aliases), category=e.category,
                  id=existing.id if existing else "",
                  translations={} if e.translation is None else {norm_lang(target): e.translation})
    return library.upsert(entry)


__all__ = ["CANDIDATES_FILE", "REVIEW_FILE", "STATUSES", "Review", "ReviewEntity", "ReviewError",
           "ReviewOccurrence", "add_to_library", "apply_update", "approved_terms", "auto_approve", "carry_over", "entity_id",
           "load_review", "merge", "save_review", "save_to_library"]
