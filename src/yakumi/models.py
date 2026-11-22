from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Segment:
    """一条带时间轴的字幕。时间单位为秒。"""

    start: float
    end: float
    text: str
    translation: str = ""
    # 所属句子的编号（sentences.json 里的 id）；-1 表示未分组，例如导入的字幕或旧任务
    group: int = -1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        return cls(
            start=float(data["start"]),
            end=float(data["end"]),
            text=str(data.get("text", "")),
            translation=str(data.get("translation", "")),
            group=int(data.get("group", -1)),
        )


def segments_to_list(segments: list[Segment]) -> list[dict[str, Any]]:
    return [s.to_dict() for s in segments]


def segments_from_list(items: list[dict[str, Any]]) -> list[Segment]:
    return [Segment.from_dict(i) for i in items]


STAGES = ["extract", "vad", "transcribe", "merge", "repair", "segment", "translate", "write", "burn"]
# 阶段或产物格式变化时加一。旧任务载入时按 pipeline.MIGRATIONS 迁移，没有对应迁移规则时从头重跑
PIPELINE_VERSION = 4


@dataclass
class JobState:
    """任务状态，持久化为任务目录下的 state.json。"""

    id: str
    video: str
    target_lang: str
    source_lang: str | None = None
    bilingual: bool = False
    burn: bool = False
    format: str = "srt"
    status: str = "pending"  # pending | running | done | error
    stage: str | None = None
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    completed: list[str] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    # 翻译时选用的术语表（ID，按顺序合并）和只对这个任务有效的补充信息
    glossaries: list[str] = field(default_factory=list)
    notes: str = ""
    # 识别模式、请求次数、费用等统计
    stats: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    pipeline_version: int = 1
    stage_inputs: dict[str, str] | None = None
    revision: int = 0
    # media：从视频或音频识别；subtitles：导入已有字幕（跳过识别和断句，可以没有视频，此时 video 为空）
    source_type: str = "media"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobState":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
