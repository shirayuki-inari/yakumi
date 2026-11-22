"""背景资料：可保存、在多个任务里复用的翻译背景（界面和命令行里叫“背景资料”，代码和文件沿用 glossary 的名字）。

每份背景资料是 ~/.config/yakumi/glossaries/ 下的一个 TOML 文件，可以在网页或命令行（yakumi context）里管理，
也可以直接编辑：

    version = 2
    name = "hololive"
    notes = \"\"\"
    VTuber 直播。主播称呼粉丝为“助手くん”。
    \"\"\"

    [[entries]]
    id = "shirakami-fubuki"            # 稳定的实体标识，省略时等于 canonical
    canonical = "白上フブキ"            # 规范的原文名称
    category = "person"                 # 人物、团体、粉丝名、作品、歌曲、游戏等，见 CATEGORIES
    aliases = ["フブキ", "フブちゃん"]   # 已确认、适合长期复用的其他称呼
    translations = { "zh" = "白上吹雪", "*" = "Fubuki" }   # 按目标语言的固定译名，"*" 表示任何语言

- notes：背景说明（核心），原样写进翻译提示词。一两句话说明这是谁的什么内容；提示词已经说明原文是
  不可靠的识别结果，模型知道背景后能自己认出被写错的名字、统一译法，通常不需要逐条列固定译法；
- entries：固定译法（可选），以实体为单位。翻译时只把在这一批原文里出现过的实体放进提示词。
  译名为空字符串表示保留原文；没有某种语言的译名时，只告诉模型这是专有名词，由它决定译法。
  aliases 只放确认过的称呼；识别错误的写法（例如把名字识别成同音字）只记在任务的术语审核结果里。

旧版文件（没有 version，固定译法写在 [terms] 里，“原文 = 译文”）照常读取：每条成为一个实体，
译名记为 "*"（不限定语言），保存时改写为新格式。译名相同的旧条目只作为合并建议，不自动视为同一实体。

任务可以选用多份背景资料，再加上只对这个任务有效的补充说明；配置文件里的 [translate] notes 和
[translate.glossary]（旧格式的“原文 = 译文”）对所有任务生效。实体按 id（或规范名称）合并，
后者覆盖前者：配置 → 背景资料（按选择的顺序）→ 任务的术语审核结果。
"""

from __future__ import annotations

import json
import os
import re
import threading
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .files import config_dir

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_NAME = 60
MAX_TEXT = 200
FORMAT_VERSION = 2
ANY_LANG = "*"

CATEGORIES = {
    "person": "人物", "group": "团体", "fan_name": "粉丝名", "work": "作品", "song": "歌曲", "game": "游戏",
    "place": "地名", "term": "用语", "other": "其他",
}


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


_LANG_ALIASES = {
    "zh": "zh", "zh-cn": "zh", "zh-hans": "zh", "zh-sg": "zh", "zh-hans-cn": "zh",
    "zh-tw": "zh-TW", "zh-hant": "zh-TW", "zh-hk": "zh-TW", "zh-mo": "zh-TW", "zh-hant-tw": "zh-TW",
}


def norm_lang(code: str | None) -> str:
    """统一语言代码：zh / zh-CN / zh-Hans → zh，zh-TW / zh-Hant → zh-TW，其他转为小写；空值或 * 表示任何语言。"""
    code = (code or "").strip().replace("_", "-")
    if not code or code == ANY_LANG:
        return ANY_LANG
    low = code.lower()
    return _LANG_ALIASES.get(low, low)


def norm_text(text: str) -> str:
    """比较称呼时用：全角半角统一（NFKC）、不区分大小写、忽略空白。"""
    return "".join(unicodedata.normalize("NFKC", text).lower().split())


def _clean(value: Any, label: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise GlossaryError(f"{label}的格式不正确")
    value = value.strip()
    if len(value) > limit or "\n" in value:
        raise GlossaryError(f"{label}太长或包含换行：{value[:30]}")
    return value


@dataclass
class Entry:
    """一个实体：规范名称、已确认的别名、按语言的固定译名。"""

    canonical: str
    translations: dict[str, str] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    category: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        self.id = self.id or self.canonical
        self.aliases = [a for a in dict.fromkeys(self.aliases) if a and a != self.canonical]

    def surfaces(self) -> list[str]:
        return [self.canonical, *self.aliases]

    def translation_for(self, lang: str | None) -> str | None:
        """目标语言的译名：先找这种语言，再找 "*"。没有时返回 None；空字符串表示保留原文。"""
        lang = norm_lang(lang)
        if lang in self.translations:
            return self.translations[lang]
        return self.translations.get(ANY_LANG)

    def merged(self, other: "Entry") -> "Entry":
        """other 覆盖 self：译名按语言覆盖，别名合并，类别以后者为准（为空时保留前者）。"""
        aliases = [a for a in [*self.surfaces(), *other.aliases] if a != other.canonical]
        return Entry(canonical=other.canonical, translations={**self.translations, **other.translations},
                     aliases=aliases, category=other.category or self.category, id=self.id)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "canonical": self.canonical, "category": self.category,
                "aliases": list(self.aliases), "translations": dict(self.translations)}

    @classmethod
    def from_dict(cls, data: Any) -> "Entry":
        if not isinstance(data, dict):
            raise GlossaryError("固定译法的格式不正确")
        canonical = _clean(data.get("canonical", ""), "名称")
        if not canonical:
            raise GlossaryError("固定译法的名称不能为空")
        aliases_in = data.get("aliases") or []
        if isinstance(aliases_in, str):  # 网页提交的“、”或逗号分隔的文字
            aliases_in = re.split(r"[、,，;；\n]", aliases_in)
        if not isinstance(aliases_in, list):
            raise GlossaryError(f"“{canonical}”的别名格式不正确")
        aliases = [a for a in (_clean(a, "别名") for a in aliases_in) if a]
        translations_in = data.get("translations") or {}
        if isinstance(translations_in, list):  # [{lang, text}]
            translations_in = {str(t.get("lang") or ""): t.get("text", "") for t in translations_in if isinstance(t, dict)}
        if not isinstance(translations_in, dict):
            raise GlossaryError(f"“{canonical}”的译名格式不正确")
        translations: dict[str, str] = {}
        for lang, text in translations_in.items():
            if text is None:
                continue
            translations[norm_lang(str(lang))] = _clean(text, "译名")
        category = _clean(data.get("category") or "", "类别", 40)
        entry_id = _clean(data.get("id") or "", "条目标识", MAX_TEXT)
        return cls(canonical=canonical, translations=translations, aliases=aliases, category=category,
                   id=entry_id)


def entries_from_terms(terms: dict[str, str]) -> list[Entry]:
    """旧格式“原文 = 译文”：每条成为一个实体，译名不限定语言。译文为空表示保留原文。"""
    out = []
    for src, dst in terms.items():
        src, dst = str(src).strip(), str(dst if dst is not None else "").strip()
        if src:
            out.append(Entry(canonical=src, translations={ANY_LANG: dst}))
    return out


def entries_from_any(value: Any) -> list[Entry]:
    """配置或调用方给出的固定译法：旧格式的 {原文: 译文}、Entry 或条目字典的列表。"""
    if not value:
        return []
    if isinstance(value, dict):
        return entries_from_terms(value)
    if isinstance(value, list):
        return [v if isinstance(v, Entry) else Entry.from_dict(v) for v in value]
    raise GlossaryError("固定译法的格式不正确")


def check_unique(entries: list[Entry]) -> None:
    seen: set[tuple[str, str]] = set()
    for e in entries:
        for label, key in (("标识", e.id), ("名称", e.canonical)):
            if (label, key) in seen:
                raise GlossaryError(f"固定译法的{label}重复：{e.canonical}")
            seen.add((label, key))


@dataclass
class Glossary:
    name: str
    entries: list[Entry] = field(default_factory=list)
    notes: str = ""
    legacy: bool = False  # 从旧版 [terms] 读取；保存时改写为新格式

    @property
    def id(self) -> str:
        return glossary_id(self.name)

    def find(self, surface: str) -> Entry | None:
        """按 id、规范名称或别名查找实体。"""
        key = norm_text(surface)
        for e in self.entries:
            if e.id == surface or key in (norm_text(s) for s in e.surfaces()):
                return e
        return None

    def upsert(self, entry: Entry) -> Entry:
        """加入实体；已有同一 id 或同名的实体时合并（新的覆盖旧的）。"""
        for i, e in enumerate(self.entries):
            if e.id == entry.id or norm_text(e.canonical) == norm_text(entry.canonical):
                self.entries[i] = e.merged(entry)
                return self.entries[i]
        self.entries.append(entry)
        return entry

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "notes": self.notes, "legacy": self.legacy,
                "entries": [e.to_dict() for e in self.entries], "suggestions": merge_suggestions(self.entries)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Glossary":
        name = str(data.get("name", "")).strip()
        terms_in = data.get("terms") or {}
        if isinstance(terms_in, list):  # 旧网页提交的 [{source, target}] 形式
            if not all(isinstance(t, dict) for t in terms_in):
                raise GlossaryError("固定译法的格式不正确")
            terms_in = {str(t.get("source") or ""): str(t.get("target") or "") for t in terms_in}
        elif not isinstance(terms_in, dict):
            raise GlossaryError("固定译法的格式不正确")
        entries_in = data.get("entries") or []
        if not isinstance(entries_in, list):
            raise GlossaryError("固定译法的格式不正确")
        entries = [Entry.from_dict(e) for e in entries_in] + entries_from_terms(terms_in)
        check_unique(entries)
        glossary_id(name)  # 校验名称
        return cls(name=name, entries=entries, notes=str(data.get("notes", "")).strip(),
                   legacy=bool(terms_in) and not entries_in)


def merge_suggestions(entries: list[Entry]) -> list[dict[str, Any]]:
    """译名相同的实体可能是同一人物的不同称呼，列出来供用户决定是否合并（不自动合并）。"""
    groups: dict[tuple[str, str], list[str]] = {}
    for e in entries:
        for lang, text in e.translations.items():
            if text:
                groups.setdefault((lang, text), []).append(e.id)
    out, seen = [], set()
    for (lang, text), ids in groups.items():
        if len(ids) > 1 and tuple(ids) not in seen:
            seen.add(tuple(ids))
            out.append({"translation": text, "lang": lang, "ids": ids})
    return out


def merge_into(g: Glossary, target_id: str, source_ids: list[str]) -> Entry:
    """把几个实体合并成一个：其他实体的名称和别名成为目标实体的别名，译名以目标实体为准。"""
    target = next((e for e in g.entries if e.id == target_id), None)
    if target is None:
        raise GlossaryError(f"找不到固定译法：{target_id}")
    for sid in source_ids:
        src = next((e for e in g.entries if e.id == sid), None)
        if src is None or src is target:
            continue
        target.aliases = [a for a in dict.fromkeys([*target.aliases, *src.surfaces()]) if a != target.canonical]
        target.translations = {**src.translations, **target.translations}
        target.category = target.category or src.category
        g.entries.remove(src)
    return target


def _toml_str(value: str) -> str:
    # JSON 的字符串转义是 TOML 基本字符串的子集
    return json.dumps(value, ensure_ascii=False)


def dumps(g: Glossary) -> str:
    lines = [f"version = {FORMAT_VERSION}", f"name = {_toml_str(g.name)}"]
    if g.notes:
        if "'''" in g.notes:
            lines.append(f"notes = {_toml_str(g.notes)}")
        else:
            lines.append(f"notes = '''\n{g.notes}\n'''")
    for e in g.entries:
        lines += ["", "[[entries]]"]
        if e.id != e.canonical:
            lines.append(f"id = {_toml_str(e.id)}")
        lines.append(f"canonical = {_toml_str(e.canonical)}")
        if e.category:
            lines.append(f"category = {_toml_str(e.category)}")
        if e.aliases:
            lines.append("aliases = [" + ", ".join(_toml_str(a) for a in e.aliases) + "]")
        if e.translations:
            lines.append("translations = { " + ", ".join(f"{_toml_str(k)} = {_toml_str(v)}"
                                                         for k, v in e.translations.items()) + " }")
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
    if int(data.get("version", 1) or 1) > FORMAT_VERSION:
        raise GlossaryError(f"背景资料 {path} 来自更新版本的 Yakumi，请升级后再使用")
    data.setdefault("name", gid)
    try:
        g = Glossary.from_dict(data)
    except GlossaryError as exc:
        raise GlossaryError(f"背景资料 {path}：{exc}") from exc
    if g.id != gid:  # 手动改过 name 时以文件名为准
        g.name = gid
    return g


def save(g: Glossary, root: Path | None = None, old_id: str | None = None) -> Glossary:
    """保存（新建或覆盖）为新格式。改名时传入原来的 ID，会删除旧文件。"""
    root = root or glossaries_dir()
    root.mkdir(parents=True, exist_ok=True)
    check_unique(g.entries)
    path = path_of(g.id, root)
    if old_id and old_id != g.id:
        if path.exists():
            raise GlossaryError(f"已经有名为“{g.name}”的背景资料")
        path_of(old_id, root).unlink(missing_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(dumps(g), encoding="utf-8")
    os.replace(tmp, path)
    g.legacy = False
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


def merge_entries(layers: Iterable[Iterable[Entry]], id_map: dict[str, str] | None = None) -> list[Entry]:
    """按顺序合并多层实体，后者覆盖前者。同一 id 或同一规范名称视为同一实体。

    id_map 不为 None 时，记下每个输入实体的 id 合并后对应哪个 id（出现记录要据此改指向）。
    """
    merged: list[Entry] = []
    index: dict[str, int] = {}
    positions: dict[str, int] = {}
    for layer in layers:
        for e in layer:
            pos = index.get("id:" + e.id, index.get("name:" + norm_text(e.canonical)))
            if pos is None:
                merged.append(Entry(**{**e.__dict__, "aliases": list(e.aliases),
                                       "translations": dict(e.translations)}))
                pos = len(merged) - 1
            else:
                merged[pos] = merged[pos].merged(e)
            index["id:" + e.id] = pos
            index["name:" + norm_text(e.canonical)] = pos
            positions[e.id] = pos
    if id_map is not None:
        id_map.update({eid: merged[pos].id for eid, pos in positions.items()})
    return merged


def resolve(cfg_terms: Any, ids: list[str], notes: str = "",
            root: Path | None = None) -> tuple[list[Entry], str]:
    """合并配置、选用的背景资料和任务的补充说明，得到翻译时使用的实体和背景说明。"""
    layers = [entries_from_any(cfg_terms)]
    sections = []
    for gid in ids:
        g = load(gid, root)
        layers.append(g.entries)
        if g.notes:
            sections.append(f"【{g.name}】\n{g.notes}" if len(ids) > 1 or notes else g.notes)
    if notes.strip():
        sections.append(f"【本视频】\n{notes.strip()}" if sections else notes.strip())
    return merge_entries(layers), "\n\n".join(sections)


# ---- 翻译时的匹配 ---------------------------------------------------------
_WORDY = re.compile(r"[A-Za-z0-9]")


def find_surface(surface: str, text: str, start: int = 0) -> int:
    """在 text 里找 surface（拉丁文字不区分大小写，且要求前后不是字母数字，避免匹配到单词的一部分）。"""
    if not surface:
        return -1
    if not _WORDY.search(surface):
        return text.find(surface, start)
    low, key = text.lower(), surface.lower()
    pos = low.find(key, start)
    while pos >= 0:
        before = low[pos - 1] if pos > 0 else ""
        after = low[pos + len(key)] if pos + len(key) < len(low) else ""
        if not (before.isascii() and before.isalnum()) and not (after.isascii() and after.isalnum()):
            return pos
        pos = low.find(key, pos + 1)
    return -1


class TermSet:
    """翻译时使用的实体，以及任务审核得到的“某句里的某个片段指向某个实体”的记录。

    每批翻译只附上与这一批相关的实体：原文里出现了规范名称或别名的，以及审核记录落在这一批句子里的。
    """

    def __init__(self, entries: list[Entry] | None = None, occurrences: list[dict[str, Any]] | None = None):
        self.entries = {e.id: e for e in (entries or [])}
        self.by_sentence: dict[int, list[tuple[str, str]]] = {}
        for occ in occurrences or []:
            if occ.get("entity") in self.entries:
                self.by_sentence.setdefault(int(occ["sentence_id"]), []).append((str(occ["surface"]), occ["entity"]))
        self.used: set[str] = set()
        self._lock = threading.Lock()

    def __bool__(self) -> bool:
        return bool(self.entries)

    def hints(self, texts: list[str], ids: list[int] | None, target: str | None) -> list[str]:
        """这一批（texts 为各行原文，ids 为对应的句子编号）需要遵守的术语，每项一行。"""
        if not self.entries:
            return []
        joined = "\n".join(texts)
        found: dict[str, list[str]] = {}  # 实体 → 原文里出现的称呼
        for e in self.entries.values():
            names = [s for s in e.surfaces() if find_surface(s, joined) >= 0]
            if names:
                found[e.id] = names
        located: dict[str, dict[str, list[int]]] = {}  # 实体 → 审核确认的片段 → 行号
        for line_no, (text, sid) in enumerate(zip(texts, ids or []), 1):
            for surface, eid in self.by_sentence.get(sid, []) if sid is not None else []:
                if surface in text:  # 原文被校对改过时，这条记录不再适用
                    located.setdefault(eid, {}).setdefault(surface, []).append(line_no)
        lines = []
        for eid in dict.fromkeys([*found, *located]):
            e = self.entries[eid]
            lines.append(self._describe(e, found.get(eid, []), target))
            for surface, nos in located.get(eid, {}).items():
                if norm_text(surface) in (norm_text(s) for s in e.surfaces()):
                    continue  # 就是规范名称或别名，上面已经说明
                where = "、".join(map(str, dict.fromkeys(nos)))
                lines.append(f"- 第 {where} 行的「{surface}」指「{e.canonical}」，按它翻译")
        with self._lock:
            self.used.update(found)
            self.used.update(located)
        return lines

    @staticmethod
    def _describe(e: Entry, names: list[str], target: str | None) -> str:
        others = [n for n in names if n != e.canonical]
        head = e.canonical + (f"（也写作 {'、'.join(others)}）" if others else "")
        tr = e.translation_for(target)
        if tr is None:
            kind = CATEGORIES.get(e.category, "")
            return f"- {head}：专有名词{f'（{kind}）' if kind else ''}，全片译法保持一致"
        if tr == "":
            return f"- {head}（保留原文，不要翻译）"
        return f"- {head} → {tr}"

    def block(self, texts: list[str], ids: list[int] | None, target: str | None) -> str:
        lines = self.hints(texts, ids, target)
        return "本批涉及的术语（译名必须遵守）：\n" + "\n".join(lines) if lines else ""

    def used_entries(self) -> list[Entry]:
        return [self.entries[i] for i in self.entries if i in self.used]
