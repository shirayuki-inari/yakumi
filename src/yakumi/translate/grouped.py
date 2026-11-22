"""整句翻译：以句子为单位翻译，再把译文按原文的字幕条分段。

请求里每个句子一行，句内的字幕条用 ¦ 隔开：
    3|音が¦ちょっと¦微妙かな。
要求模型整句翻译，并用同样数量的 ¦ 把译文分段，每段对应同一时段的原文：
    3|声音¦好像有点¦微妙吧。

逐级兜底，保证每条字幕都有译文、且与时间轴一一对应：
1. 编号齐全、每句段数与字幕条数一致 → 直接采用；
2. 不一致的句子单独重试一次；
3. 仍不一致：整句翻译（不分段），再按原文各段的字数比例，在译文的标点或词边界处切开；
4. 切不开（例如译文太短）：逐条翻译。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..models import Segment
from ..segment.common import ends_sentence
from ..text import is_cjk, join_texts
from .base import Progress, Translator, parse_numbered

SEP = "¦"
ALT_SEPS = ("｜", "|")
MAX_CUES_PER_SENTENCE = 12
_PUNCT = set("，。！？、；：…—～,.!?;:")

GROUPED_RULES = [
    "输入每行是一句话，格式为 “编号|原文”。一句话在屏幕上分成几条字幕依次显示时，各条之间用 ¦ 隔开。",
    "输出必须逐行对应，格式为 “编号|译文”：",
    "- 整句翻译，译文要通顺自然，符合译文语言的语序，不要逐段硬译；",
    "- 用 ¦ 把译文分成与原文相同数量的段，每段尽量对应同一位置原文的意思。语序不同、无法逐段对应时，"
    "可以把内容挪到相邻的段里，但段数必须与原文一致，而且每段都不能为空；",
    "- 原文没有 ¦ 的行，译文也不要出现 ¦；",
    "- 行数和编号与输入完全一致，不要合并、拆分或遗漏任何一行，不要输出其他内容。",
    "格式示例（A、B、C 代表原文的三段，a、b、c 代表对应的译文）：输入 “3|A¦B¦C”，输出 “3|a¦b¦c”。",
]


def group_cues(cues: list[Segment], max_cues: int = MAX_CUES_PER_SENTENCE) -> list[list[int]]:
    """按 group 把连续的字幕条归为句子；未分组（-1）的字幕条各自成句。"""
    groups: list[list[int]] = []
    for idx, cue in enumerate(cues):
        if (groups and cue.group >= 0 and cues[groups[-1][-1]].group == cue.group
                and len(groups[-1]) < max_cues):
            groups[-1].append(idx)
        else:
            groups.append([idx])
    return groups


def group_by_punctuation(cues: list[Segment], max_cues: int = 6, max_gap: float = 2.0) -> None:
    """已有字幕没有句子信息：把连续的字幕条按句末标点并成句子（就地设置 group）。"""
    gid = 0
    run = 0
    for idx, cue in enumerate(cues):
        if idx > 0:
            prev = cues[idx - 1]
            if ends_sentence(prev.text) or run >= max_cues or cue.start - prev.end > max_gap:
                gid += 1
                run = 0
        cue.group = gid
        run += 1


def split_parts(value: str, n: int) -> list[str] | None:
    value = value.strip()
    if not value:
        return None
    if n == 1:
        pieces = [p.strip() for p in value.split(SEP)]
        return [join_texts([p for p in pieces if p])]
    for sep in (SEP, *ALT_SEPS):
        parts = [p.strip() for p in value.split(sep)]
        if len(parts) == n and all(parts):
            return parts
    return None


def split_translation(text: str, weights: list[int]) -> list[str] | None:
    """把整句译文按原文各段的字数比例切成 len(weights) 段，切在标点或词边界处。切不开时返回 None。"""
    text = text.strip()
    n = len(weights)
    if n == 1:
        return [text] if text else None
    cjk = is_cjk(text)
    # 允许的切点：切在 p 之前。中日韩文字任意两字之间（不让标点出现在段首），其他语言只在空格处
    if cjk:
        allowed = [p for p in range(1, len(text)) if text[p] not in _PUNCT and not text[p].isspace()]
    else:
        allowed = [p + 1 for p in range(len(text) - 1) if text[p] == " "]
    # 优先切在标点之后（拉丁文字是“标点 + 空格”之后）
    preferred = {p for p in allowed if text[p - 1] in _PUNCT or (text[p - 1] == " " and p > 1 and text[p - 2] in _PUNCT)}
    total_w = sum(max(1, w) for w in weights)
    cuts: list[int] = []
    acc = 0
    for k in range(n - 1):
        acc += max(1, weights[k])
        target = len(text) * acc / total_w
        lo = cuts[-1] if cuts else 0
        remaining = n - 2 - k  # 这一刀之后还需要的切点数
        cands = [p for p in allowed if p > lo]
        cands = cands[:len(cands) - remaining] if remaining else cands
        if not cands:
            return None
        window = max(2.0, len(text) / n * 0.5)
        near = [p for p in cands if p in preferred and abs(p - target) <= window]
        cuts.append(min(near or cands, key=lambda p: abs(p - target)))
    edges = [0, *cuts, len(text)]
    parts = [text[a:b].strip() for a, b in zip(edges, edges[1:])]
    return parts if all(parts) else None


class SentenceTranslator:
    """在 Translator 之上做整句翻译。counters 记录各级兜底的触发次数。"""

    def __init__(self, translator: Translator, batch_sentences: int = 15, max_cues: int = 40):
        self.tr = translator
        self.batch_sentences = max(1, int(batch_sentences))
        self.max_cues = max(MAX_CUES_PER_SENTENCE, int(max_cues))
        self.counters = {"sentences": 0, "split_retries": 0, "split_proportional": 0, "split_per_cue": 0}
        self._lock = threading.Lock()

    def _count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counters[key] += n

    def _batches(self, groups: list[list[int]]) -> list[list[list[int]]]:
        batches: list[list[list[int]]] = []
        cues = 0
        for g in groups:
            if batches and len(batches[-1]) < self.batch_sentences and cues + len(g) <= self.max_cues:
                batches[-1].append(g)
                cues += len(g)
            else:
                batches.append([g])
                cues = len(g)
        return batches

    def _run_batch(self, cues: list[Segment], batch: list[list[int]], context: list[tuple[str, str | None]],
                   systems: tuple[str, str]) -> list[list[str]]:
        grouped_sys, plain_sys = systems
        texts = [[cues[i].text.replace(SEP, " ") for i in g] for g in batch]
        sids = [cues[g[0]].group for g in batch]  # 句子编号，用来匹配任务审核的术语记录
        results: list[list[str] | None] = [None] * len(batch)
        pending = list(range(len(batch)))
        for attempt in range(2):
            if not pending:
                break
            lines = [SEP.join(texts[p]) for p in pending]
            terms = self.tr.term_block([join_texts(texts[p]) for p in pending], [sids[p] for p in pending])
            parsed = parse_numbered(self.tr.complete(grouped_sys, self.tr.batch_prompt(lines, context, terms)))
            still = []
            for n, p in enumerate(pending, 1):
                parts = split_parts(parsed.get(n, ""), len(batch[p]))
                if parts is None:
                    still.append(p)
                else:
                    results[p] = parts
            if attempt == 0 and still:
                self._count("split_retries", len(still))
            pending = still
        if pending:
            whole = self.tr.run_batch(plain_sys, [join_texts(texts[p]) for p in pending], context,
                                      [sids[p] for p in pending])
            for p, sentence in zip(pending, whole):
                parts = split_translation(sentence, [len(t) for t in texts[p]])
                if parts is not None:
                    self._count("split_proportional")
                else:
                    parts = self.tr.run_batch(plain_sys, texts[p], context, [sids[p]] * len(texts[p]))
                    self._count("split_per_cue")
                results[p] = parts
        return [r or [] for r in results]

    def translate(self, cues: list[Segment], target: str, source: str | None = None,
                  progress: Progress | None = None) -> list[str]:
        self.tr.prepare(target)
        systems = (self.tr.build_prompt(target, source, GROUPED_RULES), self.tr.system_prompt(target, source))
        groups = group_cues(cues)
        self._count("sentences", len(groups))
        batches = self._batches(groups)
        out: list[str] = [""] * len(cues)
        total = len(cues)
        ctx_n = self.tr.context_lines

        def source_of(g: list[int]) -> str:
            return join_texts([cues[i].text for i in g])

        def store(batch: list[list[int]], parts: list[list[str]]) -> None:
            for g, ps in zip(batch, parts):
                for i, t in zip(g, ps):
                    out[i] = t

        if self.tr.concurrency == 1 or len(batches) == 1:
            history: list[tuple[str, str | None]] = []
            done = 0
            for batch in batches:
                parts = self._run_batch(cues, batch, history[-ctx_n:] if ctx_n else [], systems)
                store(batch, parts)
                history.extend((source_of(g), join_texts(ps)) for g, ps in zip(batch, parts))
                done += sum(len(g) for g in batch)
                if progress:
                    progress(done, total)
            return out

        done = 0
        lock = threading.Lock()

        def work(b: int) -> None:
            nonlocal done
            batch = batches[b]
            prev = [g for bb in batches[max(0, b - 1):b] for g in bb][-ctx_n:] if ctx_n and b else []
            context: list[tuple[str, str | None]] = [(source_of(g), None) for g in prev]
            store(batch, self._run_batch(cues, batch, context, systems))
            with lock:
                done += sum(len(g) for g in batch)
                if progress:
                    progress(done, total)

        with ThreadPoolExecutor(max_workers=self.tr.concurrency, thread_name_prefix="yakumi-tr") as pool:
            list(pool.map(work, range(len(batches))))
        return out


def translate_cues(translator: Translator, cues: list[Segment], target: str, source: str | None = None,
                   unit: str = "sentence", batch_sentences: int = 15,
                   progress: Progress | None = None) -> tuple[list[str], dict[str, Any]]:
    """按配置的翻译单位翻译字幕条，返回每条的译文和兜底计数。"""
    if unit == "cue":
        return translator.translate([c.text for c in cues], target, source, progress=progress,
                                    ids=[c.group for c in cues]), {}
    if unit != "sentence":
        raise ValueError(f"未知的翻译单位：{unit}（可选 sentence / cue）")
    st = SentenceTranslator(translator, batch_sentences)
    texts = st.translate(cues, target, source, progress=progress)
    return texts, st.counters


__all__ = ["SEP", "SentenceTranslator", "group_by_punctuation", "group_cues", "split_parts",
           "split_translation", "translate_cues"]
