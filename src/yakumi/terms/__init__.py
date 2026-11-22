"""智能术语发现与审核：翻译前找出识别文本里的专有名词，人工审核后统一译名。

    scanner.py    分块、调用模型、提取候选
    validator.py  结构和原文定位校验
    resolver.py   去重、别名及实体关联
    review.py     审核数据、状态持久化、用于翻译与保存到背景资料

流水线里对应 term_scan（扫描 → 校验 → 关联，写出 term_candidates.json 和 term_review.json）和
term_review（等待审核，任务状态为 awaiting_review）两个阶段，见 pipeline.py。
"""

from __future__ import annotations

from typing import Any

from ..config import inherited_llm_config, is_local_service
from ..models import Segment
from .scanner import FakeTermChat, TermScanner
from .validator import SentenceText

MODES = ("off", "review", "auto")


def sentences_from_cues(cues: list[Segment]) -> list[SentenceText]:
    """按字幕条的 group 拼出句子。导入的字幕和识别结果都适用；句子编号就是翻译时使用的 group。"""
    from ..text import join_texts

    out: list[SentenceText] = []
    groups: dict[int, list[Segment]] = {}
    order: list[int] = []
    for idx, cue in enumerate(cues):
        gid = cue.group if cue.group >= 0 else -(idx + 1)  # 未分组的字幕条各自成句（不参与位置记录）
        if gid not in groups:
            groups[gid] = []
            order.append(gid)
        groups[gid].append(cue)
    for gid in order:
        if gid < 0:
            continue
        items = groups[gid]
        out.append(SentenceText(gid, join_texts([c.text for c in items]), items[0].start, items[-1].end))
    return out


def terms_llm_config(cfg: dict[str, Any]) -> dict[str, Any]:
    return inherited_llm_config(cfg, "terms")


def create_scanner(cfg: dict[str, Any]) -> TermScanner:
    tcfg = cfg.get("terms", {})
    lcfg = terms_llm_config(cfg)
    common = {"chunk_chars": int(tcfg.get("chunk_chars", 4000)), "overlap_chars": int(tcfg.get("overlap_chars", 200)),
              "concurrency": int(tcfg.get("concurrency", 4))}
    if lcfg.get("provider") == "fake":
        return TermScanner(FakeTermChat(), **common)
    from ..llm import LLMError, chat_from_config

    if lcfg.get("provider", "openai") != "openai":
        raise LLMError(f"术语扫描不支持这种服务：{lcfg.get('provider')}")
    if not (lcfg.get("api_key") or is_local_service(lcfg.get("base_url", ""))):
        raise LLMError("术语扫描没有可用的密钥：请在设置里填写，或设置 terms.api_key / 翻译密钥")
    chat = chat_from_config(lcfg, effort_key="terms.reasoning_effort", label="术语扫描")
    return TermScanner(chat, **common)


__all__ = ["MODES", "create_scanner", "sentences_from_cues", "terms_llm_config"]
