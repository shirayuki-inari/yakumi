"""LLM 断句：让模型在识别文本里插入换行，把它切成完整的句子。

核心不变量：文字一个字都不能变。模型只负责“在哪里断”，句子的文字和时间都取自原来的词：
1. 把词序列切成约 chunk_chars 字的块（切在“强边界”上），各块并发请求；
2. 模型逐行输出句子。去掉空白后与原文比较：完全一致时直接换算换行位置；只有极少差异
   （相似度 ≥ 0.98，例如全角半角、省略号写法）时按 diff 的对应关系映射回原文；
   差异更大或请求失败时，这一块改用规则断句；
3. 换行位置对齐到最近的词边界，得到每句的词区间；
4. 块与块之间：前一块最后一句没有句末标点、且与下一块紧接着时，两句并成一句。
"""

from __future__ import annotations

import bisect
import threading
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from typing import Any, Callable

from ..asr.types import Word
from ..llm import LLMError
from .common import Span, cap_spans, check_spans, ends_sentence, gap_after, join_texts
from .rules import rule_spans

MIN_RATIO = 0.98
STRONG_PAUSE = 2.0  # 任意停顿超过它都算强边界
SENTENCE_PAUSE = 1.0  # 句末标点后停顿超过它算强边界
JOIN_PAUSE = 1.0  # 跨块合并：块末尾没有句末标点、且离下一块不到这么久时合并

SYSTEM_PROMPT = """你是字幕断句助手。用户会给出一段语音识别得到的文本，请按语义把它切分成句子，每行输出一句。
规则：
1. 只能在句子之间插入换行。不得增加、删除或改写任何字符，标点也不能改，错别字也保持原样。
2. 每句表达一个相对完整的意思。口语里常常缺少标点，或者一句话被标点错误地断开，请按意思判断句子边界。
3. 语气词、感叹词、打招呼、简短的回应可以单独成句。
4. 每句尽量不超过 {max_chars} 个字；过长的句子在分句处断开。
5. 只输出切分后的文本，不要编号、引号、解释或其他任何内容。"""


def plan_chunks(words: list[Word], chunk_chars: int) -> list[Span]:
    """把词序列切成约 chunk_chars 字的块。

    在目标长度的 0.5～1.5 倍之间找切点：优先“强边界”（句末标点后停顿 ≥ 1 秒，或任意停顿 ≥ 2 秒）中
    最接近目标长度的；没有强边界时选句末标点和停顿最长的位置。
    """
    n = len(words)
    chunks: list[Span] = []
    i = 0
    while i < n:
        total, k = 0, i
        cands: list[tuple[int, int]] = []
        while k < n and total < chunk_chars * 1.5:
            total += len(words[k].text)
            k += 1
            if total >= chunk_chars * 0.5 and k < n:
                cands.append((k, total))
        if k >= n or not cands:
            chunks.append((i, n) if k >= n else (i, k))
            i = k
            continue

        def key(c: tuple[int, int]) -> tuple:
            kk, t = c
            g = gap_after(words, kk - 1)
            end = ends_sentence(words[kk - 1].text)
            strong = (end and g >= SENTENCE_PAUSE) or g >= STRONG_PAUSE
            return (1, 0, 0.0, -abs(t - chunk_chars)) if strong else (0, int(end), round(g, 1), -abs(t - chunk_chars))

        cut = max(cands, key=key)[0]
        chunks.append((i, cut))
        i = cut
    return chunks


def _clean_lines(reply: str) -> list[str]:
    lines = []
    for line in reply.splitlines():
        line = "".join(line.split())
        if line and not line.startswith("```"):
            lines.append(line)
    return lines


def map_breaks(texts: list[str], reply: str) -> list[int] | None:
    """把模型输出的换行映射为词边界（切在第 k 个词之前，0 < k < len(texts)）。文字被改动太多时返回 None。"""
    pieces = ["".join(t.split()) for t in texts]
    src = "".join(pieces)
    lines = _clean_lines(reply)
    out = "".join(lines)
    if not out:
        return None
    positions, acc = [], 0
    for line in lines[:-1]:
        acc += len(line)
        positions.append(acc)
    if out != src:
        matcher = SequenceMatcher(None, src, out, autojunk=False)
        if matcher.ratio() < MIN_RATIO:
            return None
        ops = matcher.get_opcodes()

        def to_src(p: int) -> int:
            for tag, i1, i2, j1, j2 in ops:
                if j1 <= p < j2 or (p == j2 and tag == "equal"):
                    if tag == "equal":
                        return i1 + (p - j1)
                    return i1 + round((p - j1) * (i2 - i1) / max(1, j2 - j1))
            return len(src) if p >= len(out) else 0

        positions = [to_src(p) for p in positions]
    bounds = [0]
    for piece in pieces:
        bounds.append(bounds[-1] + len(piece))
    cuts = set()
    for p in positions:
        idx = bisect.bisect_left(bounds, p)
        if idx >= len(bounds):
            idx = len(bounds) - 1
        elif idx > 0 and p - bounds[idx - 1] < bounds[idx] - p:
            idx -= 1  # 离左边的词边界更近
        if 0 < idx < len(texts):
            cuts.add(idx)
    return sorted(cuts)


class LLMSegmenter:
    name = "llm"

    def __init__(self, chat: Any, chunk_chars: int = 400, max_sentence_chars: int = 60,
                 concurrency: int = 4):
        self.chat = chat
        self.chunk_chars = max(50, int(chunk_chars))
        self.max_sentence_chars = max(10, int(max_sentence_chars))
        self.concurrency = max(1, int(concurrency))
        self.system = SYSTEM_PROMPT.format(max_chars=self.max_sentence_chars)
        self.stats = {"chunks": 0, "fallbacks": 0}
        self._lock = threading.Lock()

    @property
    def hard_limit(self) -> int:
        return self.max_sentence_chars * 2

    @property
    def usage(self) -> dict[str, float] | None:
        return getattr(self.chat, "usage", None)

    def _chunk(self, words: list[Word], span: Span) -> tuple[list[Span], LLMError | None, bool]:
        i, j = span
        texts = [w.text for w in words[i:j]]
        error: LLMError | None = None
        cuts = None
        try:
            cuts = map_breaks(texts, self.chat.complete(self.system, join_texts(texts)))
        except LLMError as exc:  # 请求失败：这一块改用规则断句（程序错误照常抛出）
            error = exc
        if cuts is None:
            return rule_spans(words, self.hard_limit, start=i, end=j), error, False
        edges = [i] + [i + c for c in cuts] + [j]
        return list(zip(edges, edges[1:])), None, True

    def split(self, words: list[Word], progress: Callable[[int, int], None] | None = None) -> list[Span]:
        if not words:
            return []
        chunks = plan_chunks(words, self.chunk_chars)
        done = 0

        def work(span: Span) -> tuple[list[Span], LLMError | None, bool]:
            nonlocal done
            result = self._chunk(words, span)
            with self._lock:
                done += 1
                if progress:
                    progress(done, len(chunks))
            return result

        if self.concurrency == 1 or len(chunks) == 1:
            results = [work(c) for c in chunks]
        else:
            with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="yakumi-seg") as pool:
                results = list(pool.map(work, chunks))
        errors = [e for _, e, _ in results if e is not None]
        if errors and len(errors) == len(chunks):
            raise errors[0]  # 全部失败多半是密钥或网络问题，直接报错，不悄悄退回规则
        self.stats["chunks"] += len(chunks)
        self.stats["fallbacks"] += sum(1 for _, _, ok in results if not ok)

        spans: list[Span] = []
        for chunk_spans, _, _ in results:
            if spans and chunk_spans:
                (a, b), (_, d) = spans[-1], chunk_spans[0]
                if not ends_sentence(words[b - 1].text) and gap_after(words, b - 1) < JOIN_PAUSE:
                    spans[-1] = (a, d)
                    chunk_spans = chunk_spans[1:]
            spans.extend(chunk_spans)
        spans = cap_spans(words, spans, self.hard_limit)
        check_spans(spans, len(words))
        return spans


class RuleSegmenter:
    name = "rules"
    usage = None

    def __init__(self, max_sentence_chars: int = 60):
        self.max_sentence_chars = max(10, int(max_sentence_chars))
        self.stats: dict[str, int] = {}

    def split(self, words: list[Word], progress: Callable[[int, int], None] | None = None) -> list[Span]:
        spans = rule_spans(words, self.max_sentence_chars * 2)
        check_spans(spans, len(words))
        return spans


class FakeChat:
    """测试与调试用：在句末标点和每 12 个字之后换行，不调用任何服务。"""

    usage = None

    def complete(self, system: str, user: str) -> str:
        out, run = [], 0
        for ch in user:
            out.append(ch)
            run += 1
            if ch in "。！？.!?" or run >= 12:
                out.append("\n")
                run = 0
        return "".join(out)
