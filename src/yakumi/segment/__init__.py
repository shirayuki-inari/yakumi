"""断句：把识别结果切成句子（翻译单位），再把句子拆成字幕条（显示单位）。"""

from __future__ import annotations

from typing import Any, Callable

from ..asr.types import Word
from ..models import Segment
from .common import Sentence, check_spans, join_texts, tokenize
from .cues import build_cues, text_cues
from .llm import FakeChat, LLMSegmenter, RuleSegmenter
from .timing import place_sentences

Progress = Callable[[int, int], None]

# 断句沿用 [translate] 的连接设置，[segment] 里非空的同名项覆盖它
INHERITED = ("provider", "base_url", "model", "api_key", "routing", "provider_routing", "timeout",
              "reasoning_effort", "temperature")


def segment_llm_config(cfg: dict[str, Any]) -> dict[str, Any]:
    scfg = cfg.get("segment", {})
    merged = dict(cfg.get("translate", {}))
    for key in INHERITED:
        if scfg.get(key) not in (None, "", {}):
            merged[key] = scfg[key]
    return merged


def create_segmenter(cfg: dict[str, Any]) -> LLMSegmenter | RuleSegmenter:
    scfg = cfg.get("segment", {})
    max_chars = int(scfg.get("max_sentence_chars", 60))
    if scfg.get("mode", "llm") == "rules":
        return RuleSegmenter(max_chars)
    if scfg.get("mode", "llm") != "llm":
        raise ValueError(f"未知的断句方式：{scfg.get('mode')}（可选 llm / rules）")
    lcfg = segment_llm_config(cfg)
    common = {"chunk_chars": int(scfg.get("chunk_chars", 400)), "max_sentence_chars": max_chars,
              "concurrency": int(scfg.get("concurrency", 4))}
    if lcfg.get("provider") == "fake":
        return LLMSegmenter(FakeChat(), **common)
    local = any(h in lcfg.get("base_url", "") for h in ("127.0.0.1", "localhost"))
    if lcfg.get("provider", "openai") != "openai" or not (lcfg.get("api_key") or local):
        return RuleSegmenter(max_chars)  # 没有可用的模型接口时只能用规则
    from ..llm import chat_from_config

    chat = chat_from_config(lcfg, effort_key="segment.reasoning_effort", label="断句")
    return LLMSegmenter(chat, **common)


def segment_words(words: list[Word], segmenter: LLMSegmenter | RuleSegmenter, sub: dict[str, Any],
                  cue_gap: float, progress: Progress | None = None) -> tuple[list[Sentence], list[Segment]]:
    spans = segmenter.split(words, progress)
    check_spans(spans, len(words))
    sentences = [Sentence(k, words[i].start, words[j - 1].end, join_texts([w.text for w in words[i:j]]),
                          [i, j], segmenter.name) for k, (i, j) in enumerate(spans)]
    cues = build_cues(words, spans, cue_gap=cue_gap, max_chars=int(sub["max_chars"]),
                      max_chars_cjk=int(sub["max_chars_cjk"]), max_duration=float(sub["max_duration"]),
                      min_duration=float(sub["min_duration"]))
    return sentences, cues


def segment_blocks(blocks: list[tuple[str, tuple[float, float]]], speech: list[tuple[float, float]],
                   segmenter: LLMSegmenter | RuleSegmenter, sub: dict[str, Any],
                   progress: Progress | None = None) -> tuple[list[Sentence], list[Segment]]:
    """纯文本模式：每个窗口的文本单独断句，句子按字数比例铺到语音区间上。"""
    placed: list[Segment] = []
    for idx, (text, core) in enumerate(blocks):
        tokens = tokenize(text)
        if not tokens:
            continue
        spans = segmenter.split(tokens)
        texts = [join_texts([t.text for t in tokens[i:j]]) for i, j in spans]
        for seg in place_sentences(texts, core, speech):
            seg.group = len(placed)
            placed.append(seg)
        if progress:
            progress(idx + 1, len(blocks))
    sentences = [Sentence(s.group, s.start, s.end, s.text, None, segmenter.name) for s in placed]
    cues = text_cues(placed, max_chars=int(sub["max_chars"]), max_chars_cjk=int(sub["max_chars_cjk"]),
                     max_duration=float(sub["max_duration"]), min_duration=float(sub["min_duration"]))
    return sentences, cues


__all__ = ["INHERITED", "Sentence", "create_segmenter", "segment_words", "segment_blocks", "segment_llm_config"]
