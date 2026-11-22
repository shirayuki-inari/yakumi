"""把任务统计（识别、断句、翻译的请求次数、兜底和费用）整理成摘要，供命令行和网页共用。

`stats_report` 给出分项的结构化结果（网页按行展示），`format_stats` 把它拼成命令行用的一行文字。
"""

from __future__ import annotations

from typing import Any

Item = dict[str, Any]


def _item(text: str, warn: bool = False) -> Item:
    return {"text": text, "warn": warn}


def stats_report(stats: dict[str, Any]) -> dict[str, Any] | None:
    """分项摘要：`sections` 每项是 {label, items: [{text, warn}]}（warn 标出失败的请求），
    `cost` 是总费用，`cost_notes` 是费用的附注。"""
    if not any(stats.get(k) for k in ("asr_requests", "translate_requests", "segment_mode")):
        return None
    sections = []
    if stats.get("asr_requests"):
        sections.append({"label": "识别", "items": _asr_items(stats)})
    if stats.get("segment_mode"):
        sections.append({"label": "断句", "items": _segment_items(stats)})
    if "term_entities" in stats:
        items = [_item(f"{stats['term_entities']} 个术语")]
        if stats.get("term_invalid"):
            items.append(_item(f"{stats['term_invalid']} 条结果在原文里找不到"))
        sections.append({"label": "术语扫描", "items": items})
    if stats.get("translate_requests"):
        sections.append({"label": "翻译", "items": _translate_items(stats)})
    total = sum(stats.get(f"{k}_cost", 0) for k in ("asr", "repair", "segment", "terms", "translate"))
    notes = []
    unknown = sum(stats.get(f"{k}_cost_unknown", 0) for k in ("asr", "segment", "terms", "translate"))
    if unknown:
        notes.append(f"另有 {unknown:g} 次未返回费用")
    if any(stats.get(f"{k}_failed") for k in ("asr", "repair", "segment", "terms", "translate")):
        notes.append("失败请求的费用未计入")
    return {"sections": sections, "cost": f"${total:.4f}", "cost_notes": notes}


def format_stats(stats: dict[str, Any]) -> str:
    report = stats_report(stats)
    if not report:
        return ""
    lines = [f"{s['label']}：" + "，".join(i["text"] for i in s["items"]) for s in report["sections"]]
    cost = f"费用 {report['cost']}" + "".join(f"（{n}）" for n in report["cost_notes"])
    return "；".join(lines + [cost])


def _segment_items(stats: dict[str, Any]) -> list[Item]:
    llm = stats["segment_mode"] == "llm"
    items = [_item("LLM" if llm else "规则"), _item(f"{stats.get('sentences', 0)} 句"),
             _item(f"{stats.get('cues', 0)} 条字幕")]
    if llm and stats.get("segment_fallbacks"):
        items.append(_item(f"{stats['segment_fallbacks']}/{stats.get('segment_chunks', 0)} 块改用规则"))
    return items


def _translate_items(stats: dict[str, Any]) -> list[Item]:
    items = []
    unit = {"sentence": "整句", "cue": "逐条"}.get(stats.get("translate_unit", ""))
    if unit:
        items.append(_item(unit))
    items.append(_item(f"{stats['translate_requests']:g} 次请求"))
    items.append(_item(f"思考档位 {stats.get('translate_effort', '默认')}"))
    tokens = stats.get("translate_reasoning_tokens", 0)
    if tokens:
        items.append(_item(f"思考 {tokens / 1000:.1f}k token"))
    fallbacks = [(stats.get("translate_split_retries", 0), "重试分段"),
                 (stats.get("translate_split_proportional", 0), "按比例切分"),
                 (stats.get("translate_split_per_cue", 0), "逐条翻译")]
    items += [_item(f"{label} {n} 句") for n, label in fallbacks if n]
    if stats.get("translate_failed"):
        items.append(_item(f"失败 {stats['translate_failed']:g} 次", warn=True))
    return items


def _asr_items(stats: dict[str, Any]) -> list[Item]:
    mode = {"words": "词级时间戳", "text": "纯文本"}.get(stats.get("mode", ""), stats.get("mode", ""))
    items = [_item(mode), _item(f"{stats.get('windows', 1)} 个窗口"), _item(f"{stats['asr_requests']} 次请求")]
    if stats.get("asr_failed"):
        items.append(_item(f"失败 {stats['asr_failed']} 次", warn=True))
    if stats.get("splits"):
        items.append(_item(f"拆分 {stats['splits']} 次"))
    if "repairs_found" in stats:
        if stats["repairs_found"]:
            tried = stats.get("repairs_tried", stats.get("repair_requests", 0))
            items.append(_item(f"复核 {tried} 处，采用 {stats.get('repairs_accepted', 0)} 处"))
        else:
            items.append(_item("边界无需复核"))
    return items
