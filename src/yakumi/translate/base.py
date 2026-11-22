"""翻译的公共逻辑：分批、编号、校验与回退。

每批若干行，按 "编号|原文" 发给模型，要求按同样的编号逐行返回译文。
返回的编号缺失、多出或为空时整批重试一次；仍然不对就逐行单独翻译，
保证译文与字幕时间轴一一对应。

并发（concurrency > 1）时多批同时请求。此时上一批的译文还没出来，上下文只给上一批的原文；
人名和专有名词的一致性依靠背景说明和固定译法。concurrency = 1 时上下文同时给出原文和译文。
"""

from __future__ import annotations

import re
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from ..glossary import relevant_terms
from ..llm import LLMError

LANG_NAMES = {
    "zh": "简体中文", "zh-cn": "简体中文", "zh-hans": "简体中文",
    "zh-tw": "繁體中文", "zh-hant": "繁體中文",
    "en": "English", "ja": "日本語", "ko": "한국어", "fr": "Français", "de": "Deutsch",
    "es": "Español", "pt": "Português", "ru": "Русский", "it": "Italiano", "vi": "Tiếng Việt",
    "th": "ไทย", "id": "Bahasa Indonesia", "ar": "العربية",
}

_LINE_RE = re.compile(r"^\s*(\d+)\s*[|｜]\s?(.*)$")
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)  # 不经过 ChatClient 的实现（测试替身等）也能去掉思考过程

Progress = Callable[[int, int], None]


# 告诉模型原文并不可靠：识别错误要按本意翻译，而不是照字面硬译
SOURCE_NOTES = {
    "asr": "原文是自动语音识别（ASR）的结果，并不可靠：可能有同音误写、错字、漏字、多余的字，"
           "断句和标点也可能不对，人名、昵称和专有名词尤其容易写错。请结合上下文和背景说明判断说话人的本意，"
           "明显的识别错误直接按正确的意思翻译；实在无法判断时按字面翻译。不要在译文里指出或解释这些错误。",
    "subtitle": "原文是已有的字幕，可能来自语音识别，也可能有错字或误写。遇到明显的错误，"
                "请结合上下文按正确的意思翻译，不要在译文里指出或解释。",
}


class TranslateError(LLMError):
    pass


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code.lower(), code)


def parse_numbered(text: str) -> dict[int, str]:
    result: dict[int, str] = {}
    for line in _THINK_RE.sub("", text).splitlines():
        match = _LINE_RE.match(line)
        if match:
            result[int(match.group(1))] = match.group(2).strip()
    return result


class Translator(ABC):
    def __init__(self, batch_size: int = 30, style: str = "", glossary: dict[str, str] | None = None,
                 context_lines: int = 3, concurrency: int = 1, notes: str = "", source_kind: str = "asr"):
        self.batch_size = max(1, int(batch_size))
        self.style = style
        self.glossary = glossary or {}
        self.notes = notes.strip()
        self.source_kind = source_kind  # asr：原文是语音识别结果；subtitle：已有的字幕文件
        # 本次翻译实际用到的固定译法（只含原文里出现过的），由 prepare() 设置
        self.active_glossary: dict[str, str] | None = None
        self.context_lines = context_lines
        self.concurrency = max(1, int(concurrency))

    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """发送一次对话请求，返回模型输出的文本。"""

    def build_prompt(self, target: str, source: str | None, format_rules: list[str]) -> str:
        parts = [
            f"你是专业的视频字幕译者。把字幕翻译成{lang_name(target)}"
            + (f"（原文语言：{lang_name(source)}）" if source else "") + "。",
            "要求：口语化、简洁，适合在屏幕上快速阅读；结合上下文保持人名和术语一致；"
            "不要添加解释或注释。",
            SOURCE_NOTES.get(self.source_kind, SOURCE_NOTES["asr"]),
            *format_rules,
        ]
        if self.style:
            parts.append(f"风格要求：{self.style}")
        glossary = self.glossary if self.active_glossary is None else self.active_glossary
        if glossary:
            terms = "\n".join(f"- {k} → {v}" if v else f"- {k}（保留原文，不要翻译）" for k, v in glossary.items())
            parts.append(f"固定译法（必须遵守）：\n{terms}")
        if self.notes:
            parts.append(f"背景说明（帮助理解内容和人物，不要翻译、不要写进译文）：\n{self.notes}")
        return "\n".join(parts)

    def prepare(self, texts: list[str]) -> None:
        """翻译前调用：只把原文里出现过的固定译法放进提示词。"""
        self.active_glossary = relevant_terms(self.glossary, texts)

    def system_prompt(self, target: str, source: str | None) -> str:
        return self.build_prompt(target, source, [
            "输入每行格式为 “编号|原文”。输出必须逐行对应，格式为 “编号|译文”，"
            "行数和编号与输入完全一致，不要合并、拆分或遗漏任何一行，不要输出其他内容。",
        ])

    def batch_prompt(self, lines: list[str], context: list[tuple[str, str | None]]) -> str:
        out = []
        if context:
            out.append("上文（仅供参考，不要翻译）：")
            out.extend(f"{src} => {dst}" if dst is not None else src for src, dst in context)
            out.append("")
        out.append("请翻译：")
        out.extend(f"{i}|{line}" for i, line in enumerate(lines, 1))
        return "\n".join(out)

    def _translate_batch(self, system: str, lines: list[str],
                         context: list[tuple[str, str | None]]) -> list[str] | None:
        expected = set(range(1, len(lines) + 1))
        for _ in range(2):
            parsed = parse_numbered(self.complete(system, self.batch_prompt(lines, context)))
            if set(parsed) == expected and all(parsed.values()):
                return [parsed[i] for i in sorted(expected)]
        return None

    def _translate_single(self, system: str, line: str, context: list[tuple[str, str | None]]) -> str:
        reply = self.complete(system, self.batch_prompt([line], context))
        parsed = parse_numbered(reply)
        if parsed.get(1):
            return parsed[1]
        # 模型没按格式输出时，直接把整段回复当作译文；回复为空则保留原文。
        raw = _THINK_RE.sub("", reply).strip()
        return raw.split("|", 1)[-1].strip() if raw else line

    def run_batch(self, system: str, batch: list[str], context: list[tuple[str, str | None]]) -> list[str]:
        translated = self._translate_batch(system, batch, context)
        if translated is None:
            translated = []
            for line in batch:
                ctx = (context + list(zip(batch, translated)))[-self.context_lines:]
                translated.append(self._translate_single(system, line, ctx))
        return translated

    def translate(self, lines: list[str], target: str, source: str | None = None,
                  progress: Progress | None = None) -> list[str]:
        self.prepare(lines)
        system = self.system_prompt(target, source)
        total = len(lines)
        starts = list(range(0, total, self.batch_size))
        if self.concurrency == 1 or len(starts) == 1:
            results: list[str] = []
            for start in starts:
                batch = lines[start:start + self.batch_size]
                context = list(zip(lines[:start], results))[-self.context_lines:] if self.context_lines else []
                results.extend(self.run_batch(system, batch, context))
                if progress:
                    progress(len(results), total)
            return results

        done = 0
        lock = threading.Lock()

        def work(start: int) -> list[str]:
            nonlocal done
            batch = lines[start:start + self.batch_size]
            context: list[tuple[str, str | None]] = (
                [(src, None) for src in lines[max(0, start - self.context_lines):start]] if self.context_lines else [])
            out = self.run_batch(system, batch, context)
            with lock:
                done += len(batch)
                if progress:
                    progress(done, total)
            return out

        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="yakumi-tr") as pool:
            parts = list(pool.map(work, starts))
        return [t for part in parts for t in part]
