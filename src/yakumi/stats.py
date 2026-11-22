"""把任务统计（识别、断句、翻译的请求次数、兜底和费用）整理成一行中文摘要，供命令行和网页共用。"""

from __future__ import annotations

from typing import Any


def format_stats(stats: dict[str, Any]) -> str:
    if not any(stats.get(k) for k in ("asr_requests", "translate_requests", "segment_mode")):
        return ""
    sections = []
    if stats.get("asr_requests"):
        sections.append(_asr_section(stats))
    if stats.get("segment_mode"):
        sections.append(_segment_section(stats))
    if "term_entities" in stats:
        text = f"术语扫描：{stats['term_entities']} 个术语"
        if stats.get("term_invalid"):
            text += f"，{stats['term_invalid']} 条结果在原文里找不到"
        sections.append(text)
    if stats.get("translate_requests"):
        sections.append(_translate_section(stats))
    total = sum(stats.get(f"{k}_cost", 0) for k in ("asr", "repair", "segment", "terms", "translate"))
    cost = f"费用 ${total:.4f}"
    unknown = sum(stats.get(f"{k}_cost_unknown", 0) for k in ("asr", "segment", "terms", "translate"))
    if unknown:
        cost += f"（另有 {unknown:g} 次未返回费用）"
    if any(stats.get(f"{k}_failed") for k in ("asr", "repair", "segment", "terms", "translate")):
        cost += "（失败请求的费用未计入）"
    return "；".join(sections + [cost])


def _segment_section(stats: dict[str, Any]) -> str:
    mode = "LLM" if stats["segment_mode"] == "llm" else "规则"
    text = f"断句：{mode}，{stats.get('sentences', 0)} 句，{stats.get('cues', 0)} 条字幕"
    if stats["segment_mode"] == "llm" and stats.get("segment_fallbacks"):
        text += f"，{stats['segment_fallbacks']}/{stats.get('segment_chunks', 0)} 块改用规则"
    return text


def _translate_section(stats: dict[str, Any]) -> str:
    unit = {"sentence": "整句，", "cue": "逐条，"}.get(stats.get("translate_unit", ""), "")
    text = f"翻译：{unit}{stats['translate_requests']:g} 次请求，思考档位 {stats.get('translate_effort', '默认')}"
    tokens = stats.get("translate_reasoning_tokens", 0)
    if tokens:
        text += f"，思考 {tokens / 1000:.1f}k token"
    fallbacks = [(stats.get("translate_split_retries", 0), "重试分段"),
                 (stats.get("translate_split_proportional", 0), "按比例切分"),
                 (stats.get("translate_split_per_cue", 0), "逐条翻译")]
    detail = "、".join(f"{label} {n} 句" for n, label in fallbacks if n)
    if detail:
        text += f"（{detail}）"
    if stats.get("translate_failed"):
        text += f"，失败 {stats['translate_failed']:g} 次"
    return text


def _asr_section(stats: dict[str, Any]) -> str:
    mode = {"words": "词级时间戳", "text": "纯文本"}.get(stats.get("mode", ""), stats.get("mode", ""))
    parts = [f"识别：{mode}，{stats.get('windows', 1)} 个窗口，{stats['asr_requests']} 次请求"]
    if stats.get("asr_failed"):
        parts.append(f"失败 {stats['asr_failed']} 次")
    if stats.get("splits"):
        parts.append(f"拆分 {stats['splits']} 次")
    if "repairs_found" in stats:
        if stats["repairs_found"]:
            tried = stats.get("repairs_tried", stats.get("repair_requests", 0))
            parts.append(f"复核 {tried} 处，采用 {stats.get('repairs_accepted', 0)} 处")
        else:
            parts.append("边界无需复核")
    return "，".join(parts)
