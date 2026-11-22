"""字幕格式读写：SRT / VTT / ASS。

双语字幕把译文放在第一行、原文放在第二行；ASS 里原文用更小的字号。
没有译文的条目（例如只做识别）直接输出原文。
punctuation 为 "subtitle" 时按字幕惯例处理中日文标点（见 style.py），"keep" 原样输出。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import Segment
from .style import style_line

FORMATS = ("srt", "vtt", "ass")


def _ts(seconds: float, sep: str) -> str:
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _ass_ts(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _lines(seg: Segment, bilingual: bool, punctuation: str = "keep") -> list[str]:
    main = seg.translation.strip() or seg.text.strip()
    lines = [main, seg.text.strip()] if bilingual and seg.translation.strip() and seg.text.strip() else [main]
    return [style_line(line, punctuation) for line in lines]


def to_srt(segments: list[Segment], bilingual: bool = False, punctuation: str = "keep") -> str:
    blocks = []
    for i, seg in enumerate(segments, 1):
        text = "\n".join(_lines(seg, bilingual, punctuation))
        blocks.append(f"{i}\n{_ts(seg.start, ',')} --> {_ts(seg.end, ',')}\n{text}\n")
    return "\n".join(blocks)


def to_vtt(segments: list[Segment], bilingual: bool = False, punctuation: str = "keep") -> str:
    blocks = ["WEBVTT\n"]
    for seg in segments:
        text = "\n".join(_lines(seg, bilingual, punctuation))
        blocks.append(f"{_ts(seg.start, '.')} --> {_ts(seg.end, '.')}\n{text}\n")
    return "\n".join(blocks)


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def to_ass(segments: list[Segment], bilingual: bool = False, font: str = "",
           font_size: int = 18, play_res: tuple[int, int] = (384, 288), punctuation: str = "keep") -> str:
    font_name = font or "Noto Sans CJK SC"
    small = max(8, round(font_size * 0.72))
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_res[0]}\n"
        f"PlayResY: {play_res[1]}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        "0,0,0,0,100,100,0,0,1,1,0,2,10,10,12,1\n"
        f"Style: Original,{font_name},{small},&H00D8D8D8,&H000000FF,&H00000000,&H64000000,"
        "0,0,0,0,100,100,0,0,1,1,0,2,10,10,12,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for seg in segments:
        lines = _lines(seg, bilingual, punctuation)
        text = _ass_escape(lines[0])
        if len(lines) > 1:
            text += "\\N{\\rOriginal}" + _ass_escape(lines[1])
        events.append(f"Dialogue: 0,{_ass_ts(seg.start)},{_ass_ts(seg.end)},Default,,0,0,0,,{text}")
    return header + "\n".join(events) + "\n"


def render(segments: list[Segment], fmt: str, bilingual: bool = False, punctuation: str = "keep",
           **ass_opts) -> str:
    if fmt == "srt":
        return to_srt(segments, bilingual, punctuation)
    if fmt == "vtt":
        return to_vtt(segments, bilingual, punctuation)
    if fmt == "ass":
        return to_ass(segments, bilingual, punctuation=punctuation, **ass_opts)
    raise ValueError(f"不支持的字幕格式：{fmt}")


_TIME_RE = re.compile(
    r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _secs(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(content: str) -> list[Segment]:
    """解析 SRT 或 VTT。一个条目里的多行文字合并为一行原文。"""
    content = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    segments: list[Segment] = []
    for block in re.split(r"\n\s*\n", content):
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        for idx, line in enumerate(lines):
            match = _TIME_RE.search(line)
            if match:
                g = match.groups()
                text = " ".join(t.strip() for t in lines[idx + 1:])
                text = re.sub(r"<[^>]+>", "", text).strip()
                if text:
                    segments.append(Segment(_secs(*g[:4]), _secs(*g[4:]), text))
                break
    return segments


def read_subtitles(path: str | Path) -> list[Segment]:
    path = Path(path)
    if path.suffix.lower() not in (".srt", ".vtt"):
        raise ValueError("目前只能导入 SRT 或 VTT 字幕")
    return parse_srt(path.read_text(encoding="utf-8-sig"))
