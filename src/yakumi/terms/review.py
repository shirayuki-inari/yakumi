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
from ..glossary import CATEGORIES, Entry, GlossaryError, norm_lang

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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewOccurrence":
        return cls(int(data["sentence_id"]), str(data["surface"]), float(data.get("start", 0.0)),
                   str(data.get("found_by", "model")), str(data.get("status", "pending")))


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
                "entities": [e.to_dict() for e in self.entities]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Review":
        return cls(target=str(data.get("target", "")), finished=bool(data.get("finished")),
                   updated_at=float(data.get("updated_at", 0.0)),
                   entities=[ReviewEntity.from_dict(e) for e in data.get("entities", [])])


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
        e.status = update["status"]
    for key, label in (("canonical", "规范名称"), ("translation", "译名")):
        if key in update:
            value = _opt_text(update[key], label)
            if value != getattr(e, key):
                setattr(e, key, value)
                changed = e.edited = True
    if "category" in update:
        if update["category"] not in CATEGORIES:
            raise ReviewError(f"未知的类别：{update['category']}")
        changed |= e.category != update["category"]
        e.category = update["category"]
    if "aliases" in update:
        aliases = update["aliases"]
        if isinstance(aliases, str):
            aliases = [a for a in (x.strip() for x in aliases.replace("，", "、").replace(",", "、").split("、")) if a]
        if not isinstance(aliases, list) or not all(isinstance(a, str) and len(a) <= 200 and "\n" not in a
                                                    for a in aliases):
            raise ReviewError("别名的格式不正确")
        e.aliases = [a.strip() for a in dict.fromkeys(aliases) if a.strip()]
        changed = True
    if "occurrences" in update:
        items = update["occurrences"]
        if not isinstance(items, list):
            raise ReviewError("出现位置的格式不正确")
        sites = {(o.sentence_id, o.surface): o for o in e.occurrences}
        for item in items:
            if not isinstance(item, dict) or item.get("status") not in ("pending", "rejected"):
                raise ReviewError("出现位置的格式不正确")
            occ = sites.get((item.get("sentence_id"), item.get("surface")))
            if occ is None:
                raise ReviewError("找不到这个出现位置")
            occ.status = item["status"]
            changed = True
    if changed:
        e.auto = False
    return e


def merge(review: Review, target_key: str, source_keys: list[str]) -> ReviewEntity:
    """把几个实体并成一个（例如没能自动关联的同一人物的不同称呼），出现位置全部保留。"""
    target = review.find(target_key)
    for key in source_keys:
        src = review.find(key)
        if src is target:
            continue
        sites = {(o.sentence_id, o.surface) for o in target.occurrences}
        target.occurrences += [o for o in src.occurrences if (o.sentence_id, o.surface) not in sites]
        target.surfaces = list(dict.fromkeys(target.surfaces + src.surfaces))
        target.evidence = list(dict.fromkeys(target.evidence + src.evidence))[:5]
        target.canonical = target.canonical or src.canonical
        target.translation = target.translation if target.translation is not None else src.translation
        target.library_id = target.library_id or src.library_id
        review.entities.remove(src)
    target.occurrences.sort(key=lambda o: (o.start, o.sentence_id))
    target.auto, target.edited = False, True
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


def carry_over(new: Review, old: Review | None) -> None:
    """重新扫描时保留已有的审核决定（按实体的 key 对应；句子编号相同的出现位置保留单独拒绝的状态）。"""
    if old is None:
        return
    previous = {e.key: e for e in old.entities}
    for e in new.entities:
        before = previous.get(e.key)
        if before is None or (before.status == "pending" and not before.edited
                              and not any(o.status == "rejected" for o in before.occurrences)):
            continue
        if before.edited:
            e.canonical, e.translation, e.category = before.canonical, before.translation, before.category
        e.status, e.aliases, e.edited, e.auto = before.status, before.aliases, before.edited, before.auto
        e.saved_to = before.saved_to
        rejected = {(o.sentence_id, o.surface) for o in before.occurrences if o.status == "rejected"}
        for o in e.occurrences:
            if (o.sentence_id, o.surface) in rejected:
                o.status = "rejected"


# ---- 用于翻译 ---------------------------------------------------------------
def entity_id(e: ReviewEntity) -> str:
    return e.library_id or f"job:{e.key}"


def approved_terms(review: Review | None) -> tuple[list[Entry], list[dict[str, Any]]]:
    """通过的实体（任务级实体定义）和它们的出现记录，供翻译使用。"""
    if review is None:
        return [], []
    entries: list[Entry] = []
    occurrences: list[dict[str, Any]] = []
    lang = norm_lang(review.target)
    for e in review.entities:
        if e.status != "approved":
            continue
        eid = entity_id(e)
        entries.append(Entry(canonical=e.name, id=eid, category=e.category, aliases=list(e.aliases),
                             translations={} if e.translation is None else {lang: e.translation}))
        occurrences += [{"sentence_id": o.sentence_id, "surface": o.surface, "entity": eid}
                        for o in e.occurrences if o.status != "rejected"]
    return entries, occurrences


def save_to_library(e: ReviewEntity, gid: str, target: str) -> Entry:
    """把一个通过的实体保存到背景资料：规范名称、用户确认的别名、这种目标语言的译名。识别错误的写法不会保存。"""
    if e.status != "approved":
        raise ReviewError(f"“{e.name}”还没有通过审核，不能保存到背景资料")
    if not e.canonical:
        raise ReviewError(f"“{e.name}”没有规范名称，请先填写")
    try:
        g = glossary_store.load(gid)
        existing = next((x for x in g.entries if e.library_id and x.id == e.library_id), None)
        entry = Entry(canonical=e.canonical, aliases=list(e.aliases), category=e.category,
                      id=existing.id if existing else "",
                      translations={} if e.translation is None else {norm_lang(target): e.translation})
        saved = g.upsert(entry)
        glossary_store.save(g)
    except GlossaryError as exc:
        raise ReviewError(str(exc)) from exc
    if gid not in e.saved_to:
        e.saved_to.append(gid)
    return saved


__all__ = ["CANDIDATES_FILE", "REVIEW_FILE", "STATUSES", "Review", "ReviewEntity", "ReviewError",
           "ReviewOccurrence", "apply_update", "approved_terms", "auto_approve", "carry_over", "entity_id",
           "load_review", "merge", "save_review", "save_to_library"]
