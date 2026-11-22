"""任务流水线：（download →）extract → vad → transcribe → merge → repair → segment →（term_scan → term_review →）
translate → write → burn。

每个任务对应一个目录（默认 .yakumi/jobs/<id>/），各阶段的产物都落盘：

    download.<ext>   视频链接任务下载的视频（需要 yt-dlp）
    audio.wav        抽出的音频（16kHz 单声道 16 位），VAD、切片与复核都从这里读取
    vad.json         语音区间
    windows.json     识别窗口计划；pieces/ 下是每个窗口的识别结果
    merged.json      合并后的全局结果（词级模式为词，纯文本模式为按窗口的文本），含每个边界的报告
    repair.json      边界复核记录；words.json 为复核后的最终结果
    sentences.json   断句结果：句子（翻译单位）及其词区间
    aligned.json     字幕条（显示单位），每条带所属句子的编号 group
    term_candidates.json  术语扫描的候选和无效记录；term_review.json 为术语审核数据（见 terms/review.py）
    translated.json  带译文的字幕条（Web 编辑字幕时直接修改它）
    glossary.json    任务级术语表：翻译时使用的实体、审核确认的出现记录和背景说明
    subtitles.<fmt>  导出的字幕；另有 preview.vtt 供网页预览
    output.mp4       烧录字幕后的视频
    state.json       任务状态

已完成的阶段会跳过，识别阶段还会跳过已完成的窗口，所以中断后重新执行同一任务即可续跑。
术语审核没有完成时，任务停在 term_review，状态为 awaiting_review；审核完成后再次执行即从翻译继续。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterator

from . import glossary as glossary_store
from . import media
from .align import check_alignment, create_aligner
from .asr import create_asr
from .asr.types import AudioWindow, Word
from .chunking.merge import TextBlock, merge_text, merge_words
from .chunking.repair import Repairer
from .chunking.transcribe import WindowedTranscriber, WindowPlan, load_pieces, word_cache_needs_retry
from .chunking.vad import create_vad
from .files import FileBusyError, lock_file, read_json, write_json
from .llm import record_usage
from .models import PIPELINE_VERSION, STAGES, JobState, Segment, segments_from_list, segments_to_list
from .segment import create_segmenter, segment_blocks, segment_words
from .subtitle.formats import render
from .terms import create_scanner, sentences_from_cues
from .terms import review as term_review
from .terms.resolver import resolve as resolve_terms
from .terms.scanner import ScanContext
from .terms.validator import SentenceText
from .translate import create_translator
from .translate.grouped import group_by_punctuation, translate_cues

ProgressFn = Callable[[JobState], None]

# 需要读取 audio.wav 的阶段
AUDIO_STAGES = {"vad", "transcribe", "repair"}

# 各阶段在总进度里的占比
WEIGHTS = {"download": 10, "extract": 3, "vad": 4, "transcribe": 34, "merge": 1, "repair": 3, "segment": 4,
           "term_scan": 3, "term_review": 0, "translate": 34, "write": 2, "burn": 14}

# 旧版本任务的迁移：版本号 → 从哪个阶段开始重跑（之前的产物格式没变，保留）。不在表里的版本从头重跑
# 第 5 版新增带分段时间戳的计划，原有产物仍可直接使用。
MIGRATIONS = {3: "segment", 4: None}

STAGE_LABELS = {
    "download": "下载视频",
    "extract": "抽取音频",
    "vad": "检测语音",
    "transcribe": "语音识别",
    "merge": "合并识别结果",
    "repair": "边界复核",
    "segment": "断句",
    "term_scan": "术语扫描",
    "term_review": "术语审核",
    "translate": "翻译字幕",
    "write": "生成字幕",
    "burn": "烧录字幕",
}


JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class PipelineError(RuntimeError):
    pass


class JobBusyError(PipelineError):
    pass


class AwaitingReview(Exception):
    """术语审核还没有完成：任务暂停在 term_review，不是错误。"""


def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(8)


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
               stage_inputs: dict[str, str] | None = None, source_url: str = "", term_mode: str = "off") -> "Job":
        """创建任务。`move_video` 为真时把视频移入任务目录（用于网页上传的临时文件）。

        `video` 为空时，要么是没有视频的字幕任务（之后用 `import_subtitles` 导入字幕），
        要么是视频链接任务（`source_url`，由 download 阶段下载）。
        """
        root = Path(workspace) / "jobs"
        now = time.time()
        root.mkdir(parents=True, exist_ok=True)
        for _ in range(16):
            candidate = new_job_id() if job_id is None else job_id
            if not JOB_ID_RE.fullmatch(candidate):
                raise PipelineError(f"任务 ID 不合法：{candidate}")
            try:
                (root / candidate).mkdir()
            except FileExistsError:
                if job_id is not None:
                    raise PipelineError(f"任务已存在：{job_id}") from None
            else:
                break
        else:
            raise PipelineError("无法分配任务目录，请重试")
        state = JobState(
            id=candidate, video=str(Path(video).resolve()) if video else "", target_lang=target_lang or "",
            source_lang=source_lang or None, bilingual=bilingual, burn=burn, format=fmt,
            created_at=now, updated_at=now, pipeline_version=PIPELINE_VERSION,
            glossaries=list(glossaries or []), notes=notes.strip(), stage_inputs=stage_inputs,
            source_url=source_url, term_mode=term_mode if target_lang else "off",
        )
        job = cls(root, state)
        moved: Path | None = None
        try:
            if move_video and video:
                dest = job.dir / ("input" + Path(video).suffix.lower())
                shutil.move(str(video), dest)
                moved = dest
                job.state.video = str(dest.resolve())
            job.save()
        except OSError:
            if moved is not None and video is not None:
                shutil.move(str(moved), video)
            shutil.rmtree(job.dir, ignore_errors=True)
            raise
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
        elif st.pipeline_version not in MIGRATIONS:
            # 没有迁移规则的旧任务：中间产物格式不同，从头重跑
            st.completed, st.outputs, st.stats = [], {}, {}
            self.clear_transcription()
        elif "transcribe" in st.completed and word_cache_needs_retry(self.dir):
            # 旧版可能已导出了缺词结果。保留正常分窗，让缺损窗口重新识别。
            st.completed.remove("transcribe")
            self.invalidate_from("merge")
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
        if self.state.pipeline_version not in (4, PIPELINE_VERSION):
            return []  # 待迁移的任务会删除或重建这些文件，不提供给校对
        for name in ("translated.json", "aligned.json"):
            if self.path(name).is_file():
                return segments_from_list(read_json(self.path(name)))
        return []

    def save_segments(self, segments: list[Segment]) -> None:
        name = "translated.json" if self.state.target_lang else "aligned.json"
        write_json(self.path(name), segments_to_list(segments))
        self.invalidate_from("write")

    def source_segments(self) -> list[Segment]:
        """当前原文。已翻译任务的校对保存在 translated.json 中。"""
        return [Segment(c.start, c.end, c.text, group=c.group) for c in self.segments()]

    def replace_aligned(self, segments: list[Segment]) -> None:
        """重建原始字幕条时，旧译文和校对不再对应当前输入。"""
        self.path("translated.json").unlink(missing_ok=True)
        write_json(self.path("aligned.json"), segments_to_list(segments))

    def import_subtitles(self, segments: list[Segment]) -> None:
        """导入已有字幕：没有词级时间和句子信息，按句末标点把连续的字幕条并成句子，跳过识别和断句。"""
        group_by_punctuation(segments)
        self.replace_aligned(segments)
        self.state.source_type = "subtitles"
        self.state.completed = STAGES[:STAGES.index("term_scan")]
        self.state.stats = {"imported_cues": len(segments)}
        self.save()

    def set_translation_context(self, glossaries: list[str], notes: str) -> bool:
        """更换背景资料或补充说明。有变化时返回 True，并让翻译及之后的阶段重跑。"""
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
        self.state.stats.pop("language", None)

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
        stages = stages[STAGES.index("term_scan"):]
    if not state.source_url and "download" in stages:
        stages.remove("download")
    if not state.target_lang:
        stages.remove("translate")
    if not state.target_lang or state.term_mode not in ("review", "auto"):
        stages.remove("term_scan")
        stages.remove("term_review")
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
        if "merge" in stages and "merge" not in state.completed and "transcribe" in state.completed:
            if word_cache_needs_retry(job.dir):
                state.completed.remove("transcribe")
                job.save()
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
            job.save()
            done_weight += WEIGHTS[stage]
    except AwaitingReview as exc:
        state.status, state.stage, state.message = "awaiting_review", "term_review", str(exc)
        _finish(job, cfg)
        if progress:
            progress(state)
        return state
    except Exception as exc:  # 记录错误后继续抛出，由调用方决定如何展示
        state.status, state.error = "error", str(exc)
        state.message = f"{STAGE_LABELS.get(state.stage or '', '')}失败"
        job.save()
        if progress:
            progress(state)
        raise
    state.status, state.stage, state.progress = "done", None, 1.0
    state.message = "完成"
    _finish(job, cfg)
    if progress:
        progress(state)
    return state


def _finish(job: Job, cfg: dict[str, Any]) -> None:
    if not cfg.get("workspace", {}).get("keep_audio", False):
        job.path("audio.wav").unlink(missing_ok=True)  # 之后的审核、校对和重新导出都用不到它
    job.save()


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
    elif plan.mode == "segments":
        blocks = []
        for window, piece in pairs:
            for segment in segments_from_list(piece["segments"]):
                # 如果后端有硬上限，重叠区的分段按中点归属。
                if window.core_start <= (segment.start + segment.end) / 2 < window.core_end:
                    blocks.append(TextBlock(segment.start, segment.end, segment.text))
        if not blocks:
            raise PipelineError("没有识别到语音")
        reports = []
        data = {"mode": "segments", "blocks": [b.to_dict() for b in blocks]}
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
    language = job.state.source_lang or job.state.stats.get("language")
    log: list[dict[str, Any]] = []
    if rcfg.get("enabled", True):
        duration, speech = _load_vad(job)
        plan = WindowPlan.from_dict(read_json(job.path("windows.json")))
        truncated = [AudioWindow.from_dict(p["window"]) for p in load_pieces(job.dir, plan) if p.get("truncated")]
        repairer = Repairer(
            create_asr(cfg["asr"]), job.path("audio.wav"), job.dir, speech, duration, rcfg,
            language, job.state.stats,
            clip_suffix=media.upload_suffix(cfg["asr"].get("upload_format", "flac")),
            tolerance=float(cfg["chunking"].get("match_tolerance", 0.5)), report=report,
        )
        if repairer.asr.mode == "auto":
            repairer.asr.mode = "words"  # 已经确定是词级模式，不再探测
        words, log = repairer.run(words, data["boundaries"], truncated)
    aligner = create_aligner(cfg.get("alignment", {}))
    if aligner.name != "none":
        report(0.9, "校准时间戳")
        aligned = aligner.align(job.path("audio.wav"), words, language)
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
        speech = [] if data["mode"] == "segments" else _load_vad(job)[1]
        blocks = [(b.text, (b.core_start, b.core_end)) for b in (TextBlock.from_dict(x) for x in data["blocks"])]
        sentences, cues = segment_blocks(blocks, speech, segmenter, sub, progress)
    write_json(job.path("sentences.json"), [s.to_dict() for s in sentences])
    job.replace_aligned(cues)
    stats = job.state.stats
    stats["segment_mode"] = segmenter.name
    stats["sentences"], stats["cues"] = len(sentences), len(cues)
    if segmenter.name == "llm":
        stats["segment_chunks"] = segmenter.stats["chunks"]
        stats["segment_fallbacks"] = segmenter.stats["fallbacks"]
        record_usage(stats, "segment", segmenter.usage)


def term_scan_context(cfg: dict[str, Any], state: JobState, job: Job | None = None) -> ScanContext:
    name = state.video
    if job is not None and (meta := job.path("meta.json")).is_file():
        name = str(read_json(meta).get("name") or name)
    try:
        library, notes = glossary_store.resolve(cfg["translate"].get("glossary"), state.glossaries, state.notes)
    except glossary_store.GlossaryError as exc:
        raise PipelineError(str(exc)) from exc
    base_notes = str(cfg["translate"].get("notes", "") or "").strip()
    return ScanContext(target=state.target_lang, title=Path(name).stem if name else "",
                       notes="\n\n".join(n for n in (base_notes, notes) if n), known=library)


def _term_scan_digest(ctx: ScanContext, sentences: list[SentenceText]) -> str:
    data = [asdict(ctx), [[s.id, s.text] for s in sentences]]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def term_scan_context_changed(job: Job, cfg: dict[str, Any]) -> bool:
    if job.state.term_mode not in ("review", "auto") or "term_scan" not in job.state.completed:
        return False
    candidate_path = job.path(term_review.CANDIDATES_FILE)
    if not candidate_path.is_file():
        return True
    ctx = term_scan_context(cfg, job.state, job)
    return read_json(candidate_path).get("context") != _term_scan_digest(ctx, sentences_from_cues(job.source_segments()))


def _stage_term_scan(job: Job, cfg: dict[str, Any], report: StageReport) -> None:
    """扫描 → 校验 → 关联，写出候选和审核数据。重新扫描时保留已有的审核决定。"""
    sentences = sentences_from_cues(job.source_segments())
    ctx = term_scan_context(cfg, job.state, job)
    scanner = create_scanner(cfg)
    try:
        result = scanner.scan(sentences, ctx, progress=lambda d, n: report(d / max(1, n), f"术语扫描 {d}/{n}"))
    finally:
        record_usage(job.state.stats, "terms", scanner.usage)
    write_json(job.path(term_review.CANDIDATES_FILE), {
        "context": _term_scan_digest(ctx, sentences),
        "chunks": result.chunks, "failed_chunks": result.failed_chunks, "repairs": result.repairs,
        "candidates": [c.to_dict() for c in result.candidates], "invalid": result.invalid,
    })
    review = term_review.Review(target=job.state.target_lang,
                                entities=resolve_terms(result.candidates, sentences, ctx.known, job.state.target_lang))
    term_review.carry_over(review, term_review.load_review(job.dir))
    term_review.save_review(job.dir, review)
    stats = job.state.stats
    stats["term_candidates"], stats["term_entities"] = len(result.candidates), len(review.entities)
    stats["term_failed_chunks"] = result.failed_chunks
    stats["term_invalid"] = len(result.invalid) - result.failed_chunks


def _stage_term_review(job: Job, cfg: dict[str, Any], report: StageReport) -> None:
    review = term_review.load_review(job.dir)
    if review is None or review.finished:
        return
    if job.state.term_mode == "auto":
        term_review.auto_approve(review)
    elif not any(e.status == "pending" for e in review.entities):
        review.finished = True
    if review.finished:
        term_review.save_review(job.dir, review)
        return
    pending = review.summary()["pending"]
    raise AwaitingReview(f"发现 {len(review.entities)} 个术语，{pending} 个待审核")


def job_terms(job: Job) -> tuple[list[glossary_store.Entry], list[dict[str, Any]]]:
    """这个任务审核通过的术语（实体和出现记录）。没有启用术语扫描时为空。"""
    if job.state.term_mode not in ("review", "auto"):
        return [], []
    return term_review.approved_terms(term_review.load_review(job.dir))


def _stage_translate(job: Job, cfg: dict[str, Any], report: StageReport) -> None:
    segments = job.source_segments()
    if not segments:
        raise PipelineError("没有可翻译的原文字幕")
    tcfg = translation_config(cfg["translate"], job.state.glossaries, job.state.notes, job_terms(job))
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
    used = {e.id for e in translator.terms.used_entries()}
    write_json(job.path("glossary.json"), {
        "glossaries": job.state.glossaries,
        "target": job.state.target_lang,
        "entries": [{**e, "used": e["id"] in used} for e in tcfg["glossary"]],
        "occurrences": tcfg.get("term_occurrences", []),
        "notes": translator.notes,
    })
    record_translation_usage(job.state.stats, translator, tcfg, counters)


def translation_config(tcfg: dict[str, Any], glossaries: list[str], notes: str,
                       job_terms: tuple[list[Any], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """把选用的背景资料、补充说明和任务的术语审核结果并入 [translate] 配置。找不到背景资料时报错。

    实体的优先级：任务审核结果 ＞ 背景资料（后选的覆盖先选的）＞ 配置里的 [translate.glossary]。
    返回的 glossary 是实体字典的列表，term_occurrences 是审核确认的出现记录，都可以直接序列化。
    """
    try:
        entries, extra = glossary_store.resolve(tcfg.get("glossary"), glossaries, notes)
    except glossary_store.GlossaryError as exc:
        raise PipelineError(str(exc)) from exc
    job_entries, occurrences = job_terms or ([], [])
    scoped = {}
    if job_entries:
        # 审核时改成了已有实体的名称、或两个新实体同名时会合并，出现记录要指向合并后的实体
        id_map: dict[str, str] = {}
        merged = glossary_store.merge_entries([entries, job_entries], id_map)
        scoped["term_global_entries"] = [{**e.to_dict(), "id": id_map.get(e.id, e.id)} for e in entries]
        entries = merged
        occurrences = [{**o, "entity": id_map.get(o["entity"], o["entity"])} for o in occurrences]
    base_notes = str(tcfg.get("notes", "") or "").strip()
    return {**tcfg, **scoped, "glossary": [e.to_dict() for e in entries], "term_occurrences": occurrences,
            "notes": "\n\n".join(n for n in (base_notes, extra) if n)}


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
    "term_scan": _stage_term_scan,
    "term_review": _stage_term_review,
    "translate": _stage_translate,
    "write": _stage_write,
    "burn": _stage_burn,
}
