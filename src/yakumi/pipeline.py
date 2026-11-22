"""任务流水线：（download →）extract → vad → transcribe → merge → repair → segment → translate → write → burn。

每个任务对应一个目录（默认 .yakumi/jobs/<id>/），各阶段的产物都落盘：

    download.<ext>   视频链接任务下载的视频（需要 yt-dlp）
    audio.wav        抽出的音频（16kHz 单声道 16 位），VAD、切片与复核都从这里读取
    vad.json         语音区间
    windows.json     识别窗口计划；pieces/ 下是每个窗口的识别结果
    merged.json      合并后的全局结果（词级模式为词，纯文本模式为按窗口的文本），含每个边界的报告
    repair.json      边界复核记录；words.json 为复核后的最终结果
    sentences.json   断句结果：句子（翻译单位）及其词区间
    aligned.json     字幕条（显示单位），每条带所属句子的编号 group
    translated.json  带译文的字幕条（Web 编辑字幕时直接修改它）
    glossary.json    翻译时实际用到的术语和补充信息（记录用）
    subtitles.<fmt>  导出的字幕；另有 preview.vtt 供网页预览
    output.mp4       烧录字幕后的视频
    state.json       任务状态

已完成的阶段会跳过，识别阶段还会跳过已完成的窗口，所以中断后重新执行同一任务即可续跑。
"""

from __future__ import annotations

import re
import secrets
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from . import glossary as glossary_store
from . import media
from .align import check_alignment, create_aligner
from .asr import create_asr
from .asr.types import AudioWindow, Word
from .chunking.merge import TextBlock, merge_text, merge_words
from .chunking.repair import Repairer
from .chunking.transcribe import WindowedTranscriber, WindowPlan, load_pieces
from .chunking.vad import create_vad
from .files import FileBusyError, lock_file, read_json, write_json
from .llm import record_usage
from .models import PIPELINE_VERSION, STAGES, JobState, Segment, segments_from_list, segments_to_list
from .segment import create_segmenter, segment_blocks, segment_words
from .subtitle.formats import render
from .translate import create_translator
from .translate.grouped import group_by_punctuation, translate_cues

ProgressFn = Callable[[JobState], None]

# 需要读取 audio.wav 的阶段
AUDIO_STAGES = {"vad", "transcribe", "repair"}

# 各阶段在总进度里的占比
WEIGHTS = {"download": 10, "extract": 3, "vad": 4, "transcribe": 34, "merge": 1, "repair": 3, "segment": 4, "translate": 34,
           "write": 2, "burn": 14}

# 旧版本任务的迁移：版本号 → 从哪个阶段开始重跑（之前的产物格式没变，保留）。不在表里的版本从头重跑
MIGRATIONS = {3: "segment"}  # 3 → 4：断句改为 LLM 断句 + 句内拆分，识别、合并、复核的结果不变

STAGE_LABELS = {
    "download": "下载视频",
    "extract": "抽取音频",
    "vad": "检测语音",
    "transcribe": "语音识别",
    "merge": "合并识别结果",
    "repair": "边界复核",
    "segment": "断句",
    "translate": "翻译字幕",
    "write": "生成字幕",
    "burn": "烧录字幕",
}


JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class PipelineError(RuntimeError):
    pass


class JobBusyError(PipelineError):
    pass


def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


class Job:
    def __init__(self, root: Path, state: JobState):
        self.dir = root / state.id
        self.state = state

    # ---- 持久化 -------------------------------------------------------
    @classmethod
    def create(cls, workspace: str | Path, video: str | Path | None, target_lang: str | None,
               source_lang: str | None = None, bilingual: bool = False, burn: bool = False,
               fmt: str = "srt", move_video: bool = False, job_id: str | None = None,
               glossaries: list[str] | None = None, notes: str = "",
               stage_inputs: dict[str, str] | None = None, source_url: str = "") -> "Job":
        """创建任务。`move_video` 为真时把视频移入任务目录（用于网页上传的临时文件）。

        `video` 为空时，要么是没有视频的字幕任务（之后用 `import_subtitles` 导入字幕），
        要么是视频链接任务（`source_url`，由 download 阶段下载）。
        """
        root = Path(workspace) / "jobs"
        now = time.time()
        state = JobState(
            id=job_id or new_job_id(), video=str(Path(video).resolve()) if video else "", target_lang=target_lang or "",
            source_lang=source_lang or None, bilingual=bilingual, burn=burn, format=fmt,
            created_at=now, updated_at=now, pipeline_version=PIPELINE_VERSION,
            glossaries=list(glossaries or []), notes=notes.strip(), stage_inputs=stage_inputs,
            source_url=source_url,
        )
        job = cls(root, state)
        job.dir.mkdir(parents=True, exist_ok=True)
        if move_video and video:
            dest = job.dir / ("input" + Path(video).suffix.lower())
            shutil.move(str(video), dest)
            job.state.video = str(dest.resolve())
        job.save()
        return job

    @classmethod
    def load(cls, workspace: str | Path, job_id: str, *, migrate: bool = True) -> "Job":
        if not JOB_ID_RE.fullmatch(job_id):  # 任务 ID 会拼进路径，不允许 / 和 .. 之类的字符
            raise PipelineError(f"找不到任务：{job_id}")
        root = Path(workspace) / "jobs"
        path = root / job_id / "state.json"
        if not path.is_file():
            raise PipelineError(f"找不到任务：{job_id}")
        job = cls(root, JobState.from_dict(read_json(path)))
        if migrate and job.state.pipeline_version != PIPELINE_VERSION:
            with cls.lock(workspace, job_id):
                job = cls.load(workspace, job_id, migrate=False)
                job.migrate()
        return job

    @staticmethod
    @contextmanager
    def lock(workspace: str | Path, job_id: str) -> Iterator[None]:
        directory = Path(workspace) / "jobs" / job_id
        if not JOB_ID_RE.fullmatch(job_id) or not (directory / "state.json").is_file():
            raise PipelineError(f"找不到任务：{job_id}")
        try:
            with lock_file(directory / ".lock"):
                yield
        except FileBusyError as exc:
            raise JobBusyError("任务正在处理中，请稍候") from exc

    def migrate(self) -> None:
        st = self.state
        if st.pipeline_version == PIPELINE_VERSION:
            return
        keep_until = MIGRATIONS.get(st.pipeline_version)
        if keep_until:
            idx = STAGES.index(keep_until)
            st.completed = [s for s in st.completed if s in STAGES and STAGES.index(s) < idx]
            st.outputs = {}
            for name in ("aligned.json", "translated.json", "sentences.json"):
                self.path(name).unlink(missing_ok=True)
        else:
            # 没有迁移规则的旧任务：中间产物格式不同，从头重跑
            st.completed, st.outputs, st.stats = [], {}, {}
            self.clear_transcription()
        st.pipeline_version = PIPELINE_VERSION
        self.save()

    @staticmethod
    def list(workspace: str | Path) -> list[JobState]:
        root = Path(workspace) / "jobs"
        states = []
        for path in root.glob("*/state.json"):
            try:
                states.append(JobState.from_dict(read_json(path)))
            except (ValueError, TypeError, KeyError):
                continue
        return sorted(states, key=lambda s: s.created_at, reverse=True)

    def save(self) -> None:
        self.state.updated_at = time.time()
        self.state.revision += 1
        write_json(self.dir / "state.json", self.state.to_dict())

    def path(self, name: str) -> Path:
        return self.dir / name

    @property
    def subtitle_path(self) -> Path:
        return self.path(f"subtitles.{self.state.format}")

    # ---- 分段读写（Web 编辑使用）--------------------------------------
    def segments(self) -> list[Segment]:
        if self.state.pipeline_version != PIPELINE_VERSION:
            return []  # 待迁移的任务会删除或重建这些文件，不提供给校对
        for name in ("translated.json", "aligned.json"):
            if self.path(name).is_file():
                return segments_from_list(read_json(self.path(name)))
        return []

    def save_segments(self, segments: list[Segment]) -> None:
        name = "translated.json" if self.state.target_lang else "aligned.json"
        write_json(self.path(name), segments_to_list(segments))
        self.invalidate_from("write")

    def import_subtitles(self, segments: list[Segment]) -> None:
        """导入已有字幕：没有词级时间和句子信息，按句末标点把连续的字幕条并成句子，跳过识别和断句。"""
        group_by_punctuation(segments)
        write_json(self.path("aligned.json"), segments_to_list(segments))
        self.state.source_type = "subtitles"
        self.state.completed = STAGES[:STAGES.index("translate")]
        self.state.stats = {"imported_cues": len(segments)}
        self.save()

    def set_translation_context(self, glossaries: list[str], notes: str) -> bool:
        """更换术语表或补充信息。有变化时返回 True，并让翻译及之后的阶段重跑。"""
        glossaries, notes = list(glossaries), notes.strip()
        if (self.state.glossaries, self.state.notes) == (glossaries, notes):
            return False
        self.state.glossaries, self.state.notes = glossaries, notes
        if self.state.target_lang:
            self.invalidate_from("translate")
        else:
            self.save()
        return True

    def clear_transcription(self) -> None:
        """删除分窗识别的缓存（窗口计划与各窗口结果）。"""
        self.path("windows.json").unlink(missing_ok=True)
        shutil.rmtree(self.path("pieces"), ignore_errors=True)

    def invalidate_from(self, stage: str) -> None:
        idx = STAGES.index(stage)
        if idx <= STAGES.index("transcribe"):
            self.clear_transcription()
        self.state.completed = [s for s in self.state.completed if STAGES.index(s) < idx]
        if idx <= STAGES.index("write"):
            self.state.outputs.pop("subtitles", None)
        if idx <= STAGES.index("burn"):
            self.state.outputs.pop("video", None)
        self.save()


def enabled_stages(state: JobState) -> list[str]:
    """这个任务需要执行的阶段。导入的字幕跳过识别和断句；没有视频时不能烧录。"""
    stages = list(STAGES)
    if state.source_type == "subtitles":
        stages = stages[STAGES.index("translate"):]
    if not state.source_url and "download" in stages:
        stages.remove("download")
    if not state.target_lang:
        stages.remove("translate")
    if not state.burn or not (state.video or state.source_url):
        stages.remove("burn")
    return stages


def first_stage(state: JobState) -> str:
    """“从头重新处理”时从哪个阶段开始。已经下载好的链接视频不重新下载。"""
    stages = enabled_stages(state)
    if stages[0] == "download" and state.video and Path(state.video).is_file():
        return "extract"
    return stages[0]


def run(job: Job, cfg: dict[str, Any], progress: ProgressFn | None = None, force: bool = False,
        from_stage: str | None = None) -> JobState:
    """执行（或续跑）任务。`from_stage` 指定从哪个阶段开始强制重跑。"""
    state = job.state
    stages = enabled_stages(state)
    total = sum(WEIGHTS[s] for s in stages)
    done_weight = 0.0

    def report(stage: str, fraction: float, message: str = "") -> None:
        state.stage = stage
        state.progress = round(min(1.0, (done_weight + WEIGHTS[stage] * fraction) / total), 4)
        state.message = message or STAGE_LABELS[stage]
        job.save()
        if progress:
            progress(state)

    state.status, state.error = "running", None
    job.save()
    try:
        if force:
            job.invalidate_from(first_stage(state))
        elif from_stage:
            job.invalidate_from(from_stage)
        # audio.wav 在任务完成后会被删除；还有需要它的阶段没跑完时，重新抽取
        pending = [s for s in stages if s not in state.completed]
        if any(s in AUDIO_STAGES for s in pending) and not job.path("audio.wav").is_file():
            state.completed = [s for s in state.completed if s != "extract"]
        for stage in stages:
            if stage in state.completed:
                done_weight += WEIGHTS[stage]
                continue
            report(stage, 0.0)
            STAGE_FUNCS[stage](job, cfg, lambda f, m="", s=stage: report(s, f, m))
            state.completed.append(stage)
            done_weight += WEIGHTS[stage]
    except Exception as exc:  # 记录错误后继续抛出，由调用方决定如何展示
        state.status, state.error = "error", str(exc)
        state.message = f"{STAGE_LABELS.get(state.stage or '', '')}失败"
        job.save()
        if progress:
            progress(state)
        raise
    state.status, state.stage, state.progress = "done", None, 1.0
    state.message = "完成"
    if not cfg.get("workspace", {}).get("keep_audio", False):
        job.path("audio.wav").unlink(missing_ok=True)  # 之后的校对和重新导出都用不到它
    job.save()
    if progress:
        progress(state)
    return state


# ---- 各阶段 -----------------------------------------------------------
StageReport = Callable[..., None]


def _stage_download(job: Job, cfg: dict[str, Any], report: StageReport) -> None:
    from .download import DownloadError, download

    try:
        path, title = download(job.state.source_url, job.dir, cfg.get("download", {}), progress=report)
    except DownloadError as exc:
        raise PipelineError(str(exc)) from exc
    job.state.video = str(path.resolve())
    if title:  # 网页上显示视频标题
        write_json(job.path("meta.json"), {"name": f"{title}{path.suffix}", "url": job.state.source_url})


def _stage_extract(job: Job, cfg: dict[str, Any], report: StageReport) -> None:
    media.extract_audio(job.state.video, job.path("audio.wav"))


def _stage_vad(job: Job, cfg: dict[str, Any], report: StageReport) -> None:
    audio = job.path("audio.wav")
    vad = create_vad(cfg["chunking"])
    speech = vad.speech_regions(audio)
    write_json(job.path("vad.json"), {
        "vad": vad.name,
        "duration": media.probe_duration(audio),
        "speech": [[round(s, 3), round(e, 3)] for s, e in speech],
    })
    job.state.stats["vad"] = vad.name


def _load_vad(job: Job) -> tuple[float, list[tuple[float, float]]]:
    data = read_json(job.path("vad.json"))
    return float(data["duration"]), [(float(s), float(e)) for s, e in data["speech"]]


def _stage_transcribe(job: Job, cfg: dict[str, Any], report: StageReport) -> None:
    duration, speech = _load_vad(job)
    runner = WindowedTranscriber(
        create_asr(cfg["asr"]), job.path("audio.wav"), job.dir, speech, duration, cfg["chunking"],
        job.state.source_lang, job.state.stats, report=report, save_stats=job.save,
        clip_suffix=media.upload_suffix(cfg["asr"].get("upload_format", "flac")),
    )
    runner.run()


def _stage_merge(job: Job, cfg: dict[str, Any], report: StageReport) -> None:
    plan = WindowPlan.from_dict(read_json(job.path("windows.json")))
    pieces = load_pieces(job.dir, plan)
    windows = {p["window"]["index"]: p for p in pieces}
    pairs = [(w, windows[w.index]) for w in plan.windows]
    tolerance = float(cfg["chunking"].get("match_tolerance", 0.5))
    if plan.mode == "words":
        words, reports = merge_words([(w, [Word.from_dict(x) for x in p["words"]]) for w, p in pairs],
                                     tolerance, float(cfg["chunking"].get("edge_margin", 0.5)))
        if not words:
            raise PipelineError("没有识别到语音")
        data: dict[str, Any] = {"mode": "words", "words": [w.to_dict() for w in words]}
    else:
        blocks, reports = merge_text([(w, p.get("text", "")) for w, p in pairs])
        if not blocks:
            raise PipelineError("没有识别到语音")
        data = {"mode": "text", "blocks": [b.to_dict() for b in blocks]}
    data["boundaries"] = [r.to_dict() for r in reports]
    write_json(job.path("merged.json"), data)
    job.state.stats["risky_boundaries"] = sum(1 for r in reports if r.risky)


def _stage_repair(job: Job, cfg: dict[str, Any], report: StageReport) -> None:
    """边界复核，然后（可选）强制对齐。纯文本模式没有词级时间，两者都跳过。"""
    data = read_json(job.path("merged.json"))
    if data["mode"] != "words":
        write_json(job.path("words.json"), data)
        write_json(job.path("repair.json"), {"skipped": True, "repairs": []})
        return
    words = [Word.from_dict(w) for w in data["words"]]
    rcfg = cfg.get("repair", {})
    log: list[dict[str, Any]] = []
    if rcfg.get("enabled", True):
        duration, speech = _load_vad(job)
        plan = WindowPlan.from_dict(read_json(job.path("windows.json")))
        truncated = [AudioWindow.from_dict(p["window"]) for p in load_pieces(job.dir, plan) if p.get("truncated")]
        repairer = Repairer(
            create_asr(cfg["asr"]), job.path("audio.wav"), job.dir, speech, duration, rcfg,
            job.state.source_lang, job.state.stats,
            clip_suffix=media.upload_suffix(cfg["asr"].get("upload_format", "flac")),
            tolerance=float(cfg["chunking"].get("match_tolerance", 0.5)), report=report,
        )
        if repairer.asr.mode == "auto":
            repairer.asr.mode = "words"  # 已经确定是词级模式，不再探测
        words, log = repairer.run(words, data["boundaries"], truncated)
    aligner = create_aligner(cfg.get("alignment", {}))
    if aligner.name != "none":
        report(0.9, "校准时间戳")
        aligned = aligner.align(job.path("audio.wav"), words, job.state.source_lang)
        check_alignment(words, aligned)
        words = aligned
        job.state.stats["aligner"] = aligner.name
    write_json(job.path("words.json"), {**data, "words": [w.to_dict() for w in words]})
    write_json(job.path("repair.json"), {"skipped": not rcfg.get("enabled", True), "repairs": log})


def _stage_segment(job: Job, cfg: dict[str, Any], report: StageReport) -> None:
    sub = cfg["subtitle"]
    scfg = cfg.get("segment", {})
    data = read_json(job.path("words.json"))
    segmenter = create_segmenter(cfg)
    label = "LLM 断句" if segmenter.name == "llm" else "断句"

    def progress(done: int, total: int) -> None:
        report(done / max(1, total), f"{label} {done}/{total}")

    if data["mode"] == "words":
        words = [Word.from_dict(w) for w in data["words"]]
        sentences, cues = segment_words(words, segmenter, sub, float(scfg.get("cue_gap", 1.2)), progress)
    else:
        _, speech = _load_vad(job)
        blocks = [(b.text, (b.core_start, b.core_end)) for b in (TextBlock.from_dict(x) for x in data["blocks"])]
        sentences, cues = segment_blocks(blocks, speech, segmenter, sub, progress)
    write_json(job.path("sentences.json"), [s.to_dict() for s in sentences])
    write_json(job.path("aligned.json"), segments_to_list(cues))
    stats = job.state.stats
    stats["segment_mode"] = segmenter.name
    stats["sentences"], stats["cues"] = len(sentences), len(cues)
    if segmenter.name == "llm":
        stats["segment_chunks"] = segmenter.stats["chunks"]
        stats["segment_fallbacks"] = segmenter.stats["fallbacks"]
        record_usage(stats, "segment", segmenter.usage)


def _stage_translate(job: Job, cfg: dict[str, Any], report: StageReport) -> None:
    segments = segments_from_list(read_json(job.path("aligned.json")))
    tcfg = translation_config(cfg["translate"], job.state.glossaries, job.state.notes)
    if job.state.source_type == "subtitles":
        tcfg["source_kind"] = "subtitle"  # 原文是已有的字幕，而不是语音识别结果
    translator = create_translator(tcfg, job.state.target_lang)
    texts, counters = translate_cues(
        translator, segments, job.state.target_lang, job.state.source_lang,
        unit=str(tcfg.get("unit", "sentence")), batch_sentences=int(tcfg.get("batch_sentences", 15)),
        progress=lambda d, n: report(d / n, f"翻译字幕 {d}/{n}"),
    )
    for seg, text in zip(segments, texts):
        seg.translation = text
    write_json(job.path("translated.json"), segments_to_list(segments))
    write_json(job.path("glossary.json"), {
        "glossaries": job.state.glossaries,
        "terms": translator.active_glossary if translator.active_glossary is not None else translator.glossary,
        "notes": translator.notes,
    })
    record_translation_usage(job.state.stats, translator, tcfg, counters)


def translation_config(tcfg: dict[str, Any], glossaries: list[str], notes: str) -> dict[str, Any]:
    """把选用的术语表和补充信息并入 [translate] 配置。找不到术语表时报错。"""
    try:
        terms, extra = glossary_store.resolve(tcfg.get("glossary"), glossaries, notes)
    except glossary_store.GlossaryError as exc:
        raise PipelineError(str(exc)) from exc
    base_notes = str(tcfg.get("notes", "") or "").strip()
    return {**tcfg, "glossary": terms, "notes": "\n\n".join(n for n in (base_notes, extra) if n)}


def record_translation_usage(stats: dict[str, Any], translator: Any, tcfg: dict[str, Any],
                             counters: dict[str, Any] | None = None) -> None:
    """把翻译的请求数、思考 token、费用和整句翻译的兜底次数记入任务统计。"""
    record_usage(stats, "translate", getattr(translator, "usage", None))
    if getattr(translator, "usage", None):
        stats["translate_model"] = getattr(translator, "request_model", tcfg.get("model"))
        stats["translate_effort"] = tcfg.get("reasoning_effort") or "默认"
    stats["translate_unit"] = "sentence" if counters else "cue"
    for key, value in (counters or {}).items():
        stats[f"translate_{key}"] = value


def _stage_write(job: Job, cfg: dict[str, Any], report: StageReport) -> None:
    sub = cfg["subtitle"]
    segments = job.segments()
    ass_opts = {"font": sub.get("font", ""), "font_size": int(sub["font_size"])} if job.state.format == "ass" else {}
    punct = str(sub.get("punctuation", "subtitle"))
    job.subtitle_path.write_text(render(segments, job.state.format, job.state.bilingual, punct, **ass_opts),
                                 encoding="utf-8")
    job.path("preview.vtt").write_text(render(segments, "vtt", job.state.bilingual, punct), encoding="utf-8")
    job.state.outputs["subtitles"] = job.subtitle_path.name


def _stage_burn(job: Job, cfg: dict[str, Any], report: StageReport) -> None:
    sub = cfg["subtitle"]
    media.burn_subtitles(job.state.video, job.subtitle_path, job.path("output.mp4"),
                         font=sub.get("font", ""), font_size=int(sub["font_size"]),
                         fonts_dir=sub.get("fonts_dir", ""))
    job.state.outputs["video"] = "output.mp4"


STAGE_FUNCS: dict[str, Callable[[Job, dict[str, Any], StageReport], None]] = {
    "download": _stage_download,
    "extract": _stage_extract,
    "vad": _stage_vad,
    "transcribe": _stage_transcribe,
    "merge": _stage_merge,
    "repair": _stage_repair,
    "segment": _stage_segment,
    "translate": _stage_translate,
    "write": _stage_write,
    "burn": _stage_burn,
}
