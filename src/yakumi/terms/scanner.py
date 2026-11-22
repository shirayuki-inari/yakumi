"""术语扫描：把识别文本分块交给模型，找出人名、昵称、作品名、粉丝称呼等需要统一翻译的专有名词。

- 按句子分块（默认约 4000 字），相邻两块重叠约 200 字，避免名字恰好落在块的边界上被漏掉；
- 请求里附上视频标题、背景说明和已知术语（优先列出这一块里出现过的），模型据此认出被识别写错的名字；
- 模型只需列出每个称呼第一次出现的句子，同一称呼在其他句子里的出现由 resolver 逐字查找补全；
- 返回的 JSON 逐项校验（validator.py）；原文里找不到的片段，每块最多做一次定向修复请求，仍不行的保留为异常记录；
- 宁可多列（低置信度也保留），由人工审核决定。模型无法确定规范写法或译名时返回 null。
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from ..glossary import CATEGORIES, Entry, find_surface
from ..llm import LLMError
from ..translate.base import lang_name
from .validator import Candidate, SentenceText, validate_item

MAX_KNOWN = 150  # 请求里最多列出的已知术语数

SYSTEM_PROMPT = """你是字幕翻译前的术语整理助手。用户会给出一段自动语音识别（ASR）得到的文本，每行格式为“编号|原文”。
请找出其中需要在翻译时统一处理的专有名词和特殊称呼：人名、昵称、自称、团体、作品、歌曲、游戏、地名、粉丝称呼、社区用语和梗等。

识别结果并不可靠：名字常被写成同音字、错字，或者被切错位置。请结合背景说明和已知术语判断哪些片段其实是专有名词。

只输出一个 JSON 对象：{{"terms": [...]}}。每项包含：
- sentence_id：所在行的编号（整数）
- surface：该行原文里的片段，必须从原文逐字复制，是连续、完整的一段；不要改正错字，不要补全或删减
- canonical：这个名称正确的原文写法；对应已知术语时使用已知术语的写法；无法确定时为 null
- translation：{target}译名；已知术语有译名时照抄；无法确定时为 null
- category：{categories} 之一
- confidence：high / medium / low
- evidence：一句话说明判断依据

规则：
1. 同一个片段在多行出现时，只列出第一次出现的那一行。
2. 不要列出普通词汇、语气词和常见名词。
3. 拿不准的也可以列出，把 confidence 设为 low；不要编造原文里没有的片段。
4. 没有找到时输出 {{"terms": []}}。只输出 JSON，不要其他内容。"""

REPAIR_PROMPT = """下面这些片段在指定的行里找不到。请对照该行原文，给出原文里实际对应的连续片段（逐字复制）；
确实没有对应的片段时，surface 为 null。只输出 JSON：{"fixes": [{"index": 1, "surface": "..."}]}"""


class ScanError(LLMError):
    pass


@dataclass
class ScanContext:
    target: str
    title: str = ""
    notes: str = ""
    known: list[Entry] = field(default_factory=list)


@dataclass
class ScanResult:
    candidates: list[Candidate]
    invalid: list[dict[str, Any]]
    chunks: int
    failed_chunks: int
    repairs: int


def plan_chunks(sentences: list[SentenceText], chunk_chars: int = 4000, overlap_chars: int = 200) -> list[tuple[int, int]]:
    """按句子分块：每块约 chunk_chars 字，下一块从上一块末尾约 overlap_chars 字处开始。返回句子下标区间 [i, j)。"""
    chunk_chars = max(200, int(chunk_chars))
    overlap_chars = max(0, min(int(overlap_chars), chunk_chars // 2))
    chunks: list[tuple[int, int]] = []
    i, n = 0, len(sentences)
    while i < n:
        total, j = 0, i
        while j < n and (total < chunk_chars or j == i):
            total += len(sentences[j].text)
            j += 1
        chunks.append((i, j))
        if j >= n:
            break
        back, k = 0, j
        while k > i + 1 and back + len(sentences[k - 1].text) <= overlap_chars:
            k -= 1
            back += len(sentences[k].text)
        i = k
    return chunks


def parse_json(reply: str, key: str) -> list[Any]:
    """从回复里取出 JSON（可能带 ```json 代码块或前后的说明文字），返回 key 对应的列表。"""
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    starts = [p for p in (text.find("{"), text.find("[")) if p >= 0]
    if not starts:
        raise ValueError("回复里没有 JSON")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    data = json.loads(text[start:end + 1])
    if isinstance(data, dict):
        data = data.get(key, [])
    if not isinstance(data, list):
        raise ValueError("JSON 格式不对")
    return data


def known_lines(known: list[Entry], text: str, target: str) -> list[str]:
    """已知术语，出现在这一块里的排在前面，最多 MAX_KNOWN 条。"""
    def describe(e: Entry) -> str:
        names = f"（又称 {'、'.join(e.aliases)}）" if e.aliases else ""
        tr = e.translation_for(target)
        tail = "" if tr is None else (" → 保留原文" if tr == "" else f" → {tr}")
        return f"- {e.canonical}{names}{tail}" + (f" [{e.category}]" if e.category else "")

    present = [e for e in known if any(find_surface(s, text) >= 0 for s in e.surfaces())]
    rest = [e for e in known if e not in present]
    return [describe(e) for e in (present + rest)[:MAX_KNOWN]]


class TermScanner:
    def __init__(self, chat: Any, chunk_chars: int = 4000, overlap_chars: int = 200, concurrency: int = 4):
        self.chat = chat
        self.chunk_chars = int(chunk_chars)
        self.overlap_chars = int(overlap_chars)
        self.concurrency = max(1, int(concurrency))
        self._lock = threading.Lock()

    @property
    def usage(self) -> dict[str, float] | None:
        return getattr(self.chat, "usage", None)

    def system_prompt(self, target: str) -> str:
        return SYSTEM_PROMPT.format(target=lang_name(target), categories=" / ".join(CATEGORIES))

    def user_prompt(self, ctx: ScanContext, lines: list[SentenceText]) -> str:
        body = "\n".join(f"{s.id}|{s.text}" for s in lines)
        parts = []
        if ctx.title:
            parts.append(f"视频标题：{ctx.title}")
        if ctx.notes:
            parts.append(f"背景说明：\n{ctx.notes}")
        known = known_lines(ctx.known, body, ctx.target)
        if known:
            parts.append("已知术语：\n" + "\n".join(known))
        parts.append("原文：\n" + body)
        return "\n\n".join(parts)

    def _ask(self, system: str, user: str, key: str) -> list[Any]:
        """请求一次；回复不是合法 JSON 时再试一次。"""
        error: Exception | None = None
        for _ in range(2):
            try:
                return parse_json(self.chat.complete(system, user), key)
            except (ValueError, json.JSONDecodeError) as exc:
                error = exc
        raise ScanError(f"术语扫描的回复无法解析：{error}")

    def _repair(self, bad: list[tuple[dict[str, Any], str]], sentences: dict[int, SentenceText],
                chunk_ids: list[int]) -> tuple[list[Candidate], list[dict[str, Any]]]:
        """对原文里找不到的片段做一次定向修复：给出那一行原文，让模型重新逐字复制。"""
        asks = [(item, sentences[int(item["sentence_id"])]) for item, _ in bad
                if isinstance(item, dict) and _int(item.get("sentence_id")) in sentences]
        fixed: list[Candidate] = []
        invalid = [{"item": item, "reason": reason} for item, reason in bad
                   if not (isinstance(item, dict) and _int(item.get("sentence_id")) in sentences)]
        if not asks:
            return fixed, invalid
        lines = [f"{n}. 第 {s.id} 行的「{item.get('surface')}」\n   原文：{s.text}" for n, (item, s) in enumerate(asks, 1)]
        try:
            fixes = {_int(f.get("index")): f.get("surface") for f in self._ask(REPAIR_PROMPT, "\n".join(lines), "fixes")
                     if isinstance(f, dict)}
        except LLMError as exc:
            return fixed, invalid + [{"item": item, "reason": f"修复失败：{exc}"} for item, _ in asks]
        for n, (item, _s) in enumerate(asks, 1):
            surface = fixes.get(n)
            cand, reason = (validate_item({**item, "surface": surface}, sentences, chunk_ids)
                            if isinstance(surface, str) and surface.strip() else (None, "修复后仍找不到"))
            if cand is None:
                invalid.append({"item": item, "reason": reason})
            else:
                cand.fixed = "repaired"
                fixed.append(cand)
        return fixed, invalid

    def _chunk(self, ctx: ScanContext, lines: list[SentenceText]) -> tuple[list[Candidate], list[dict[str, Any]], int]:
        sentences = {s.id: s for s in lines}
        ids = [s.id for s in lines]
        items = self._ask(self.system_prompt(ctx.target), self.user_prompt(ctx, lines), "terms")
        good: list[Candidate] = []
        bad: list[tuple[dict[str, Any], str]] = []
        for item in items:
            cand, reason = validate_item(item, sentences, ids)
            if cand is None:
                bad.append((item, reason))
            else:
                good.append(cand)
        repaired, invalid = self._repair(bad, sentences, ids) if bad else ([], [])
        return good + repaired, invalid, len(repaired)

    def scan(self, sentences: list[SentenceText], ctx: ScanContext,
             progress: Callable[[int, int], None] | None = None) -> ScanResult:
        chunks = plan_chunks(sentences, self.chunk_chars, self.overlap_chars)
        done = 0

        def work(span: tuple[int, int]) -> tuple[list[Candidate], list[dict[str, Any]], int] | LLMError:
            nonlocal done
            try:
                result: Any = self._chunk(ctx, sentences[span[0]:span[1]])
            except LLMError as exc:  # 这一块失败，其他块照常；全部失败时报错
                result = exc
            with self._lock:
                done += 1
                if progress:
                    progress(done, len(chunks))
            return result

        if self.concurrency == 1 or len(chunks) <= 1:
            results = [work(c) for c in chunks]
        else:
            with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="yakumi-terms") as pool:
                results = list(pool.map(work, chunks))
        errors = [r for r in results if isinstance(r, LLMError)]
        if chunks and len(errors) == len(chunks):
            raise errors[0]
        candidates: list[Candidate] = []
        invalid: list[dict[str, Any]] = []
        repairs = 0
        seen: set[tuple[int, str]] = set()
        for r in results:
            if isinstance(r, LLMError):
                invalid.append({"item": None, "reason": f"这一块扫描失败：{r}"})
                continue
            good, bad, fixed = r
            repairs += fixed
            invalid += bad
            for c in good:  # 重叠部分会被扫描两次
                if (c.sentence_id, c.surface) not in seen:
                    seen.add((c.sentence_id, c.surface))
                    candidates.append(c)
        return ScanResult(candidates, invalid, len(chunks), len(errors), repairs)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class FakeTermChat:
    """测试与调试用：把每行里第一个大写字母开头的拉丁词当作术语，不调用任何服务。"""

    usage = None

    def complete(self, system: str, user: str) -> str:
        if "fixes" in system:
            return '{"fixes": []}'
        terms = []
        seen = set()
        for line in user.split("原文：\n", 1)[-1].splitlines():
            sid, _, text = line.partition("|")
            match = re.search(r"\b[A-Z][A-Za-z0-9]+\b", text)
            if sid.strip().isdigit() and match and match.group(0) not in seen:
                seen.add(match.group(0))
                terms.append({"sentence_id": int(sid), "surface": match.group(0), "canonical": None,
                              "translation": None, "category": "other", "confidence": "medium",
                              "evidence": "测试"})
        return json.dumps({"terms": terms}, ensure_ascii=False)


__all__ = ["FakeTermChat", "ScanContext", "ScanError", "ScanResult", "TermScanner", "parse_json", "plan_chunks"]
