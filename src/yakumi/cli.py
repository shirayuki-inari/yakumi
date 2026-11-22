"""yakumi 命令行。"""

from __future__ import annotations

import functools
import hashlib
import json
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import click

from . import __version__, media
from . import glossary as glossary_store
from .config import ConfigError, load_config
from .models import STAGES, JobState
from .pipeline import (STAGE_LABELS, Job, PipelineError, job_terms, record_translation_usage, run, term_scan_context,
                       term_scan_context_changed, translation_config)
from .segment import INHERITED, segment_llm_config
from .terms import MODES as TERM_MODES, sentences_from_cues, terms_llm_config
from .terms import review as term_review
from .stats import format_stats
from .subtitle.formats import FORMATS, read_subtitles, render
from .translate import create_translator
from .translate.grouped import group_by_punctuation, translate_cues


def _cfg(ctx: click.Context, **overrides: dict[str, Any]) -> dict[str, Any]:
    try:
        return load_config(ctx.obj.get("config"), overrides=overrides,
                           warn=lambda msg: click.echo(click.style(f"! {msg}", fg="yellow"), err=True))
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


def _progress_printer():
    last = {"line": ""}
    tty = sys.stderr.isatty()

    def show(state: JobState) -> None:
        if state.status == "error":
            line = f"✗ {state.message}"
        elif state.status == "done":
            line = "✓ 完成"
        else:
            line = f"[{state.progress * 100:5.1f}%] {state.message}"
        if line == last["line"]:
            return
        last["line"] = line
        if tty:
            click.echo("\r\033[K" + line, nl=state.status in ("done", "error"), err=True)
        else:
            click.echo(line, err=True)

    return show


def _find_job(workspace: str, video: Path, target: str | None, source: str | None) -> JobState | None:
    """找最近一个同一视频、同一语言设置的任务，用于续跑或复用识别和翻译结果。"""
    video_str = str(video.resolve())
    for state in Job.list(workspace):
        if (state.video, state.target_lang, state.source_lang) == (video_str, target or "", source or None):
            return state
    return None


def _check_glossaries(ids: tuple[str, ...]) -> list[str]:
    for gid in ids:
        try:
            glossary_store.load(gid)
        except glossary_store.GlossaryError as exc:
            raise click.ClickException(f"{exc}\n用 yakumi context list 查看已有的背景资料。") from exc
    return list(ids)


# 只影响密钥、速度或客户端等待时间的设置，不改变阶段结果，不参与指纹。
# asr.timeout 会发给服务端并决定分窗，需要保留；translate.concurrency 决定上下文是否带译文，也要保留。
RUNTIME_ONLY = {
    "asr": {"api_key", "threads"},
    "segment": {"api_key", "timeout", "concurrency"},
    "terms": {"api_key", "timeout", "concurrency"},
    "translate": {"api_key", "timeout"},
}


def _stage_inputs(cfg: dict[str, Any], state: JobState, job: Job | None = None) -> dict[str, str]:
    """记录 CLI 各阶段的输入，续跑时只重做受变化影响的阶段。"""
    with open(state.video, "rb") as video:
        media_digest = hashlib.file_digest(video, "sha256").hexdigest()
    chunk = cfg["chunking"]
    vad = {key: chunk.get(key) for key in ("vad", "vad_threshold", "silence_db")}
    merge = {key: chunk.get(key) for key in ("match_tolerance", "edge_margin")}
    asr = {key: val for key, val in cfg["asr"].items() if key not in RUNTIME_ONLY["asr"]}
    asr["source_lang"] = state.source_lang
    subtitle = cfg["subtitle"]
    cue = {key: subtitle.get(key) for key in ("max_chars", "max_chars_cjk", "min_duration", "max_duration")}
    segment = {key: val for key, val in cfg["segment"].items()
               if key not in INHERITED and key not in RUNTIME_ONLY["segment"]}
    if segment.get("mode") == "llm":
        llm = segment_llm_config(cfg)
        segment.update({key: llm.get(key) for key in INHERITED if key not in RUNTIME_ONLY["segment"]})
        segment["has_key"] = bool(llm.get("api_key")) if llm.get("provider") == "openai" else False
    style = {"font": subtitle.get("font"), "font_size": subtitle.get("font_size")}
    inputs = {
        "extract": media_digest,
        "vad": vad,
        "transcribe": [asr, {key: val for key, val in chunk.items() if key not in vad and key not in merge}],
        "merge": merge,
        "repair": [cfg["repair"], cfg["alignment"]],
        "segment": [segment, cue],
        "write": [state.format, state.bilingual, subtitle.get("punctuation"), style if state.format == "ass" else None],
        "burn": [state.burn, style, subtitle.get("fonts_dir")] if state.burn else False,
    }
    if state.target_lang:
        inputs["translate"] = _translate_inputs(cfg, state, job)
        if state.term_mode in ("review", "auto"):
            scan = {key: val for key, val in cfg["terms"].items()
                    if key not in INHERITED and key not in RUNTIME_ONLY["terms"] and key != "mode"}
            llm = terms_llm_config(cfg)
            scan.update({key: llm.get(key) for key in INHERITED if key not in RUNTIME_ONLY["terms"]})
            ctx = term_scan_context(cfg, state, job)
            inputs["term_scan"] = [state.term_mode, scan, ctx.target, ctx.title, ctx.notes,
                                   [entry.to_dict() for entry in ctx.known]]
    return {stage: _digest(value) for stage, value in inputs.items()}


def _translate_inputs(cfg: dict[str, Any], state: JobState, job: Job | None) -> dict[str, Any]:
    terms = job_terms(job) if job is not None else None
    translation = translation_config(cfg["translate"], state.glossaries, state.notes, terms)
    for key in RUNTIME_ONLY["translate"]:
        translation.pop(key, None)
    translation["glossary"] = _glossary_fingerprint(translation["glossary"])
    if not translation["term_occurrences"]:
        translation.pop("term_occurrences")
    return translation


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _glossary_fingerprint(entries: list[dict[str, Any]]) -> list[Any]:
    """固定译法的指纹。只有旧格式能表达的内容（名称 → 不限语言的译名）时沿用旧的写法，
    升级后续跑旧任务不会因为指纹格式变化而重新翻译。"""
    simple = all(e["id"] == e["canonical"] and not e["aliases"] and not e["category"]
                 and set(e["translations"]) == {glossary_store.ANY_LANG} for e in entries)
    if simple:
        return [[e["canonical"], e["translations"][glossary_store.ANY_LANG]] for e in entries]
    return entries


def _update_job_inputs(job: Job, cfg: dict[str, Any], bilingual: bool, burn: bool, fmt: str,
                       glossaries: list[str] | None, notes: str | None, term_mode: str | None = None) -> None:
    st = job.state
    st.bilingual, st.burn, st.format = bilingual, burn, fmt
    if glossaries is not None:
        st.glossaries = glossaries
    if notes is not None:
        st.notes = notes.strip()
    if term_mode is not None and st.target_lang:
        st.term_mode = term_mode
    current = _stage_inputs(cfg, st, job)
    previous, st.stage_inputs = st.stage_inputs or {}, current
    changed = next((stage for stage in STAGES if previous.get(stage) != current.get(stage)), None)
    if (changed is None or STAGES.index(changed) > STAGES.index("term_scan")) and term_scan_context_changed(job, cfg):
        changed = "term_scan"
    if previous and changed:
        job.invalidate_from(changed)
        click.echo(f"任务输入有变化，将从「{STAGE_LABELS[changed]}」重新处理", err=True)
    else:
        job.save()


@contextmanager
def _prepare_job(cfg: dict[str, Any], video: Path, target: str | None, source: str | None,
                 bilingual: bool, burn: bool, fmt: str, force: bool, glossaries: list[str] | None = None,
                 notes: str | None = None, term_mode: str | None = None) -> Iterator[Job]:
    workspace = cfg["workspace"]["dir"]
    previous = None if force else _find_job(workspace, video, target, source)
    reuse = previous is not None and previous.stage_inputs is not None
    try:
        if reuse:
            job_id = previous.id
        else:
            if previous is not None:
                glossaries = previous.glossaries if glossaries is None else glossaries
                notes = previous.notes if notes is None else notes
                term_mode = previous.term_mode if term_mode is None else term_mode
            job_id = Job.create(workspace, video, target, source, bilingual, burn, fmt,
                                glossaries=glossaries, notes=notes or "", stage_inputs={},
                                term_mode=term_mode or str(cfg["terms"].get("mode", "off"))).state.id
        with Job.lock(workspace, job_id):
            job = Job.load(workspace, job_id, migrate=False)
            job.migrate()
            _update_job_inputs(job, cfg, bilingual, burn, fmt, glossaries, notes, term_mode)
            if reuse:
                completed = "、".join(STAGE_LABELS[s] for s in job.state.completed) or "无"
                click.echo(f"沿用任务 {job_id}（已完成：{completed}）", err=True)
            yield job
    except (OSError, PipelineError) as exc:
        raise click.ClickException(str(exc)) from exc


def _execute(cfg: dict[str, Any], job: Job) -> bool:
    """执行任务。停下来等待术语审核时返回 False。"""
    try:
        run(job, cfg, progress=_progress_printer())
    except Exception as exc:
        raise click.ClickException(f"{exc}\n任务目录：{job.dir}\n重新执行同一命令即可从中断处继续。") from exc
    if job.state.status == "awaiting_review":
        click.echo(f"{job.state.message}。请审核：yakumi terms review {job.state.id}\n"
                   "审核完成后重新执行同一命令，从翻译继续（也可以在网页工作台审核）。", err=True)
        return False
    st = job.state
    if st.target_lang and st.term_mode in ("review", "auto") and st.stage_inputs is not None:
        # 术语审核的结果在这次运行中才产生（自动通过等），翻译指纹按实际使用的结果更新，下次不会误判为有变化
        st.stage_inputs["translate"] = _digest(_translate_inputs(cfg, st, job))
        job.save()
    summary = format_stats(job.state.stats)
    if summary:
        click.echo(summary, err=True)
    return True


def _export(job: Job, out_dir: Path, stem: str, tag: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    exported = []
    sub_dest = out_dir / f"{stem}.{tag}.{job.state.format}"
    shutil.copyfile(job.subtitle_path, sub_dest)
    exported.append(sub_dest)
    if job.state.burn and job.path("output.mp4").is_file():
        video_dest = out_dir / f"{stem}.{tag}.mp4"
        shutil.copyfile(job.path("output.mp4"), video_dest)
        exported.append(video_dest)
    return exported


asr_option = click.option("--asr", "asr_provider", default=None,
                          help="语音识别方式：openai（兼容接口）或 whisper_cpp（本地）。")
model_option = click.option("--model", default=None, help="翻译使用的模型名，覆盖配置。")
translator_option = click.option("--translator", "translate_provider", default=None, hidden=True)
source_option = click.option("-s", "--source", default=None, help="原视频语言，如 en、ja；默认自动识别。")
format_option = click.option("--format", "fmt", type=click.Choice(FORMATS), default=None, help="字幕格式。")
notes_option = click.option("--notes", default=None,
                            help="只对这个任务有效的补充说明，会写进翻译提示词，例如“这期是第一次 ASMR 直播”。")
terms_option = click.option("--terms", "term_mode", type=click.Choice(TERM_MODES), default=None,
                            help="术语扫描：off 不扫描；review 扫描后暂停，等人工审核（yakumi terms review）；"
                                 "auto 只自动采用与背景资料对应的结果。默认取配置 terms.mode。")


def context_option(f):
    """-x / --context 选用背景资料；旧的 -g / --glossary 仍然可用（隐藏），两者合并。"""

    @functools.wraps(f)
    def wrapper(*args, legacy_contexts=(), **kwargs):
        kwargs["glossaries"] = tuple(kwargs.get("glossaries") or ()) + tuple(legacy_contexts)
        return f(*args, **kwargs)

    wrapper = click.option("-g", "--glossary", "legacy_contexts", multiple=True, hidden=True)(wrapper)
    return click.option("-x", "--context", "glossaries", multiple=True,
                        help="翻译时使用的背景资料（可以重复指定多个），见 yakumi context。")(wrapper)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-c", "--config", type=click.Path(exists=True, dir_okay=False), default=None,
              help="指定配置文件（只读取这一个文件，代替全局配置和项目配置）。")
@click.version_option(__version__, prog_name="yakumi")
@click.pass_context
def main(ctx: click.Context, config: str | None) -> None:
    """Yakumi：视频进，翻译字幕出。"""
    ctx.ensure_object(dict)
    ctx.obj["config"] = config


@main.command("run")
@click.argument("video", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-t", "--to", "target", required=True, help="目标语言，如 zh、en、ja。")
@source_option
@click.option("--bilingual/--mono", default=None, help="双语字幕（译文 + 原文）。")
@click.option("--burn", is_flag=True, help="把字幕烧录进视频。")
@format_option
@context_option
@notes_option
@terms_option
@asr_option
@model_option
@translator_option
@click.option("-o", "--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="输出目录，默认与视频同目录。")
@click.option("--force", is_flag=True, help="不复用之前的结果，从头开始。")
@click.pass_context
def run_cmd(ctx, video: Path, target, source, bilingual, burn, fmt, glossaries, notes, term_mode, asr_provider,
            model, translate_provider, out_dir, force):
    """识别、翻译并导出字幕（可选烧录）。"""
    cfg = _cfg(ctx, asr={"provider": asr_provider},
               translate={"model": model, "provider": translate_provider})
    bilingual = cfg["subtitle"]["bilingual"] if bilingual is None else bilingual
    fmt = fmt or cfg["subtitle"]["format"]
    ids = _check_glossaries(glossaries) if glossaries else None
    with _prepare_job(cfg, video, target, source, bilingual, burn, fmt, force, ids, notes, term_mode) as job:
        if not _execute(cfg, job):
            return
        for path in _export(job, out_dir or video.parent, video.stem, target):
            click.echo(str(path))


@main.command()
@click.argument("media_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@source_option
@format_option
@asr_option
@click.option("-o", "--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="输出目录，默认与输入同目录。")
@click.option("--force", is_flag=True, help="不复用之前的结果，从头开始。")
@click.pass_context
def transcribe(ctx, media_file: Path, source, fmt, asr_provider, out_dir, force):
    """只做语音识别，导出原文字幕。"""
    cfg = _cfg(ctx, asr={"provider": asr_provider})
    fmt = fmt or cfg["subtitle"]["format"]
    with _prepare_job(cfg, media_file, None, source, False, False, fmt, force) as job:
        _execute(cfg, job)
        tag = job.state.source_lang or "orig"
        for path in _export(job, out_dir or media_file.parent, media_file.stem, tag):
            click.echo(str(path))


@main.command()
@click.argument("subtitles", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-t", "--to", "target", required=True, help="目标语言。")
@source_option
@click.option("--bilingual/--mono", default=None, help="双语字幕（译文 + 原文）。")
@format_option
@context_option
@notes_option
@model_option
@translator_option
@click.option("-o", "--output", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="输出文件，默认为 <原文件名>.<语言>.<格式>。")
@click.pass_context
def translate(ctx, subtitles: Path, target, source, bilingual, fmt, glossaries, notes, model,
              translate_provider, output):
    """翻译已有的 SRT / VTT 字幕。"""
    cfg = _cfg(ctx, translate={"model": model, "provider": translate_provider})
    bilingual = cfg["subtitle"]["bilingual"] if bilingual is None else bilingual
    fmt = fmt or (output.suffix.lstrip(".") if output and output.suffix.lstrip(".") in FORMATS else cfg["subtitle"]["format"])
    try:
        segments = read_subtitles(subtitles)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not segments:
        raise click.ClickException("字幕文件里没有内容")
    try:
        tcfg = translation_config(cfg["translate"], _check_glossaries(glossaries), notes or "")
        tcfg["source_kind"] = "subtitle"
    except PipelineError as exc:
        raise click.ClickException(str(exc)) from exc
    translator = create_translator(tcfg, target)
    # 已有字幕没有词级时间，不做 LLM 断句：按句末标点把连续的字幕条并成句子再整句翻译
    group_by_punctuation(segments)

    def show(done: int, total: int) -> None:
        click.echo(f"\r\033[K翻译 {done}/{total}", nl=done == total, err=True)

    try:
        texts, counters = translate_cues(translator, segments, target, source,
                                         unit=str(tcfg.get("unit", "sentence")),
                                         batch_sentences=int(tcfg.get("batch_sentences", 15)), progress=show)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    for seg, text in zip(segments, texts):
        seg.translation = text
    stats: dict[str, Any] = {}
    record_translation_usage(stats, translator, tcfg, counters)
    if format_stats(stats):
        click.echo(format_stats(stats), err=True)
    output = output or subtitles.with_name(f"{subtitles.stem}.{target}.{fmt}")
    sub = cfg["subtitle"]
    opts = {"font": sub.get("font", ""), "font_size": int(sub["font_size"])} if fmt == "ass" else {}
    output.write_text(render(segments, fmt, bilingual, str(sub.get("punctuation", "subtitle")), **opts),
                      encoding="utf-8")
    click.echo(str(output))


@main.command()
@click.argument("video", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("subtitles", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--output", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="输出视频，默认为 <视频名>.subbed.mp4。")
@click.pass_context
def burn(ctx, video: Path, subtitles: Path, output):
    """把字幕文件烧录进视频。"""
    cfg = _cfg(ctx)
    sub = cfg["subtitle"]
    output = output or video.with_name(f"{video.stem}.subbed.mp4")
    click.echo("正在烧录字幕…", err=True)
    try:
        media.burn_subtitles(video, subtitles, output, font=sub.get("font", ""),
                             font_size=int(sub["font_size"]), fonts_dir=sub.get("fonts_dir", ""))
    except media.MediaError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(str(output))


@main.group()
def context():
    """管理背景资料：按频道、节目保存的翻译背景，可以在多个任务里复用。

    \b
    每份背景资料包含：
      背景说明  一两句话说明这是谁的什么内容，原样写进翻译提示词（核心）
      固定译法  可选，以实体为单位：规范名称、已确认的别名、按目标语言的译名
    保存在 ~/.config/yakumi/glossaries/，翻译时用 -x 选用。
    """


def _load_context(name: str) -> glossary_store.Glossary:
    try:
        return glossary_store.load(glossary_store.glossary_id(name))
    except glossary_store.GlossaryError as exc:
        raise click.ClickException(str(exc)) from exc


def _save_context(g: glossary_store.Glossary) -> None:
    try:
        glossary_store.save(g)
    except glossary_store.GlossaryError as exc:
        raise click.ClickException(str(exc)) from exc


def _describe_entry(e: glossary_store.Entry) -> str:
    langs = "，".join(f"{'任何语言' if k == glossary_store.ANY_LANG else k}：{v or '（保留原文）'}"
                     for k, v in e.translations.items()) or "（未指定译名）"
    extra = [glossary_store.CATEGORIES.get(e.category, e.category)] if e.category else []
    if e.aliases:
        extra.append("又称 " + "、".join(e.aliases))
    if e.id != e.canonical:
        extra.append(f"id {e.id}")
    return f"{e.canonical} → {langs}" + (f"  （{'；'.join(extra)}）" if extra else "")


@context.command("list")
def context_list():
    """列出所有背景资料。"""
    items = glossary_store.list_all()
    if not items:
        click.echo("还没有背景资料。用 yakumi context notes <名称> <背景说明> 创建，"
                   f"保存在 {glossary_store.glossaries_dir()}")
    for g in items:
        notes = g.notes.splitlines()[0][:30] if g.notes else "（没有背景说明）"
        click.echo(f"{g.id}  {notes}" + (f"  · {len(g.entries)} 条固定译法" if g.entries else ""))


@context.command("show")
@click.argument("name")
def context_show(name):
    """显示一份背景资料。"""
    g = _load_context(name)
    click.echo(f"# {g.name}（{glossary_store.path_of(g.id)}）" + ("  旧格式，保存时会改写为新格式" if g.legacy else ""))
    click.echo(f"\n背景说明：\n{g.notes or '（无）'}")
    click.echo(f"\n固定译法（{len(g.entries)} 条）：")
    for e in g.entries:
        click.echo(f"  {_describe_entry(e)}")
    for s in glossary_store.merge_suggestions(g.entries):
        names = "、".join(s["ids"])
        click.echo(click.style(f"\n提示：{names} 的译名都是“{s['translation']}”，如果是同一实体的不同称呼，"
                               f"可以用 yakumi context merge {g.id} {' '.join(s['ids'])} 合并", fg="yellow"))


def _get_or_new(name: str) -> glossary_store.Glossary:
    try:
        gid = glossary_store.glossary_id(name)
        if not glossary_store.path_of(gid).exists():
            return glossary_store.Glossary(name=gid, revision="")
        return glossary_store.load(gid)
    except glossary_store.GlossaryError as exc:
        raise click.ClickException(str(exc)) from exc


@context.command("notes")
@click.argument("name")
@click.argument("text", required=False)
@click.option("-f", "--file", "file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="从文件读取背景说明。")
def context_notes(name, text, file):
    """设置背景说明（背景资料不存在时自动创建）：主播、节目、人物关系、粉丝名等，原样写进翻译提示词。"""
    if file:
        text = file.read_text(encoding="utf-8")
    if text is None:
        raise click.ClickException("请提供背景说明文字，或用 -f 指定文件")
    g = _get_or_new(name)
    g.notes = text.strip()
    _save_context(g)


@context.command("set")
@click.argument("name")
@click.argument("source")
@click.argument("target", default="")
@click.option("-l", "--lang", default="*", show_default=True,
              help="译名对应的目标语言，如 zh、zh-TW、en；* 表示任何语言。")
@click.option("-a", "--alias", "aliases", multiple=True, help="已确认的其他称呼（可以重复指定）。")
@click.option("--category", type=click.Choice(list(glossary_store.CATEGORIES)), default=None, help="类别。")
def context_set(name, source, target, lang, aliases, category):
    """添加或修改一条固定译法（背景资料不存在时自动创建）。译文留空表示保留原文。

    SOURCE 是已有实体的名称或别名时，修改这个实体；否则以 SOURCE 为规范名称新建。
    """
    g = _get_or_new(name)
    entry = g.find(source.strip())
    try:
        if entry is None:
            entry = glossary_store.Entry(canonical=glossary_store._clean(source, "名称"))
            if not entry.canonical:
                raise click.ClickException("名称不能为空")
            g.entries.append(entry)
        entry.translations[glossary_store.norm_lang(lang)] = glossary_store._clean(target, "译名")
    except glossary_store.GlossaryError as exc:
        raise click.ClickException(str(exc)) from exc
    entry.aliases = [a for a in dict.fromkeys([*entry.aliases, *(x.strip() for x in aliases)])
                     if a and a != entry.canonical]
    if category:
        entry.category = category
    _save_context(g)
    click.echo(f"{g.id}：{_describe_entry(entry)}")


@context.command("unset")
@click.argument("name")
@click.argument("source")
def context_unset(name, source):
    """删除一条固定译法；SOURCE 是某个实体的别名时，只删除这个别名。"""
    g = _load_context(name)
    entry = g.find(source)
    if entry is None:
        raise click.ClickException(f"{g.id} 里没有“{source}”")
    if source in entry.aliases:
        entry.aliases.remove(source)
    else:
        g.entries.remove(entry)
    _save_context(g)


@context.command("merge")
@click.argument("name")
@click.argument("target")
@click.argument("sources", nargs=-1, required=True)
def context_merge(name, target, sources):
    """把几条固定译法合并为同一实体：SOURCES 的名称和别名成为 TARGET 的别名。"""
    g = _load_context(name)
    found = [g.find(x) for x in (target, *sources)]
    missing = [x for x, e in zip((target, *sources), found) if e is None]
    if missing:
        raise click.ClickException(f"{g.id} 里没有：{'、'.join(missing)}")
    entry = glossary_store.merge_into(g, found[0].id, [e.id for e in found[1:]])
    _save_context(g)
    click.echo(f"{g.id}：{_describe_entry(entry)}")


@context.command("import")
@click.argument("name")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def context_import(name, file):
    """从 CSV / TSV / “原文 = 译文” 文本文件导入固定译法（一行一条，译名不限定语言，追加到背景资料）。"""
    g = _get_or_new(name)
    terms = glossary_store.import_terms(file.read_text(encoding="utf-8-sig"))
    for entry in glossary_store.entries_from_terms(terms):
        g.upsert(entry)
    _save_context(g)
    click.echo(f"{g.id}：导入 {len(terms)} 条，共 {len(g.entries)} 条固定译法")


@context.command("delete")
@click.argument("name")
@click.confirmation_option(prompt="确定删除这份背景资料？")
def context_delete(name):
    """删除一份背景资料。"""
    try:
        glossary_store.delete(glossary_store.glossary_id(name))
    except glossary_store.GlossaryError as exc:
        raise click.ClickException(str(exc)) from exc


# 旧命令名 yakumi glossary 仍然可用（不在帮助里显示）
main.add_command(click.Group("glossary", commands=context.commands, hidden=True,
                             help="旧名称，请改用 yakumi context。"))


# ---- 术语审核 ---------------------------------------------------------------
STATUS_TEXT = {"pending": "待审核", "approved": "通过", "rejected": "拒绝"}


@main.group()
def terms():
    """审核任务的术语扫描结果（任务用 yakumi run --terms review 启用扫描）。

    \b
    yakumi terms show <任务>     列出扫描到的术语
    yakumi terms review <任务>   逐个审核待审核的术语
    yakumi terms set <任务> <序号> --approve / --reject / --canonical … / --translation …
    yakumi terms done <任务>     结束审核（剩下的待审核术语不使用）
    审核结束后重新执行原来的 yakumi run 命令，从翻译继续。审核结果只对这个任务有效，
    可以用 --save-to 把通过的术语保存到背景资料。
    """


@contextmanager
def _review_of(ctx: click.Context, job_id: str, write: bool = True) -> Iterator[tuple[Job, term_review.Review]]:
    cfg = _cfg(ctx)
    workspace = cfg["workspace"]["dir"]
    try:
        with Job.lock(workspace, job_id) if write else _nullcontext():
            job = Job.load(workspace, job_id, migrate=False)
            review = term_review.load_review(job.dir)
            if review is None:
                raise click.ClickException("这个任务还没有术语扫描结果（用 yakumi run --terms review 启用）")
            yield job, review
            if write:
                term_review.save_review(job.dir, review)
                job.save()
    except (PipelineError, term_review.ReviewError) as exc:
        raise click.ClickException(str(exc)) from exc


@contextmanager
def _nullcontext() -> Iterator[None]:
    yield


def _context_lines(job: Job, e: term_review.ReviewEntity, limit: int = 3) -> list[str]:
    texts = {s.id: s.text for s in sentences_from_cues(job.source_segments())}
    lines = []
    for o in e.occurrences[:limit]:
        text = texts.get(o.sentence_id, "")
        mark = text.replace(o.surface, click.style(o.surface, bold=True, underline=True), 1)
        flag = "（已排除）" if o.status == "rejected" else ""
        tenths = round(o.start * 10)  # 先取整到 0.1 秒，避免 59.96 显示成 00:60.0
        lines.append(f"    {tenths // 600:02d}:{tenths % 600 / 10:04.1f}  {mark}{flag}")
    if len(e.occurrences) > limit:
        lines.append(f"    …… 共 {len(e.occurrences)} 处")
    return lines


def _entity_line(n: int, e: term_review.ReviewEntity) -> str:
    surfaces = "、".join(e.surfaces)
    name = e.canonical or "（未确定规范名称）"
    tr = e.translation if e.translation is not None else "（未定译名）"
    tags = [glossary_store.CATEGORIES.get(e.category, e.category), f"置信度 {e.confidence}"]
    if e.library_id:
        tags.append("对应背景资料" + ("，自动通过" if e.auto else ""))
    return (f"{n:>3}. [{STATUS_TEXT[e.status]}] {surfaces} → {name} → {tr}  （{'，'.join(tags)}，"
            f"{len(e.occurrences)} 处）")


@terms.command("show")
@click.argument("job_id")
@click.option("--all", "show_all", is_flag=True, help="同时显示出现位置。")
@click.pass_context
def terms_show(ctx, job_id, show_all):
    """列出扫描到的术语和审核状态。"""
    with _review_of(ctx, job_id, write=False) as (job, review):
        s = review.summary()
        click.echo(f"共 {s['total']} 个术语：待审核 {s['pending']}，通过 {s['approved']}，拒绝 {s['rejected']}"
                   + ("；审核已结束" if review.finished else ""))
        for n, e in enumerate(review.entities, 1):
            click.echo(_entity_line(n, e))
            if show_all:
                for line in _context_lines(job, e, limit=10):
                    click.echo(line)


def _save_to(e: term_review.ReviewEntity, save_to: str | None, target: str) -> None:
    if save_to and e.status == "approved" and e.canonical:
        term_review.save_to_library(e, glossary_store.glossary_id(save_to), target)
        click.echo(f"    已保存到背景资料“{save_to}”")


@terms.command("set")
@click.argument("job_id")
@click.argument("refs", nargs=-1, required=True)
@click.option("--approve", "status", flag_value="approved", help="通过。")
@click.option("--reject", "status", flag_value="rejected", help="拒绝。")
@click.option("--pending", "status", flag_value="pending", help="改回待审核。")
@click.option("--canonical", default=None, help="规范名称。")
@click.option("--translation", default=None, help="译名。")
@click.option("--category", type=click.Choice(list(glossary_store.CATEGORIES)), default=None)
@click.option("--alias", "aliases", multiple=True, help="保存到背景资料时一并保存的已确认别名。")
@click.option("--merge-into", default=None, help="把这些术语合并到另一个术语（序号）。")
@click.option("--save-to", default=None, help="通过后保存到这份背景资料。")
@click.pass_context
def terms_set(ctx, job_id, refs, status, canonical, translation, category, aliases, merge_into, save_to):
    """修改术语（REFS 为 terms show 列出的序号，可以多个）。"""
    with _review_of(ctx, job_id) as (job, review):
        targets = [review.by_number(r) for r in refs]
        if merge_into:
            into = review.by_number(merge_into)
            term_review.merge(review, into.key, [e.key for e in targets])
            targets = [into]
        for e in targets:
            update: dict[str, Any] = {"key": e.key}
            for key, value in (("status", status), ("canonical", canonical), ("translation", translation),
                               ("category", category)):
                if value is not None:
                    update[key] = value
            if aliases:
                update["aliases"] = list(aliases)
            term_review.apply_update(review, update)
            click.echo(_entity_line(review.entities.index(e) + 1, e))
            _save_to(e, save_to, job.state.target_lang)


@terms.command("review")
@click.argument("job_id")
@click.option("--save-to", default=None, help="把通过的术语保存到这份背景资料（可以之后在其他任务里复用）。")
@click.pass_context
def terms_review(ctx, job_id, save_to):
    """逐个审核待审核的术语：通过、修改、拒绝或跳过。"""
    with _review_of(ctx, job_id) as (job, review):
        pending = [e for e in review.entities if e.status == "pending"]
        if not pending:
            click.echo("没有待审核的术语。")
        for e in pending:
            click.echo("")
            click.echo(_entity_line(review.entities.index(e) + 1, e))
            if e.evidence:
                click.echo(f"    依据：{e.evidence[0]}")
            for line in _context_lines(job, e):
                click.echo(line)
            choice = click.prompt("  [a] 通过  [e] 修改后通过  [r] 拒绝  [s] 跳过  [q] 退出",
                                  type=click.Choice(["a", "e", "r", "s", "q"]), default="a", show_choices=False)
            if choice == "q":
                break
            if choice == "s":
                continue
            update: dict[str, Any] = {"key": e.key, "status": "rejected" if choice == "r" else "approved"}
            if choice == "e":
                update["canonical"] = click.prompt("    规范名称", default=e.canonical or "", show_default=bool(e.canonical))
                update["translation"] = click.prompt("    译名", default=e.translation or "",
                                                     show_default=e.translation is not None)
            term_review.apply_update(review, update)
            _save_to(e, save_to, job.state.target_lang)
        left = review.summary()["pending"]
        if not review.finished and click.confirm(
                "结束审核？" + (f"剩下 {left} 个待审核的术语不会使用。" if left else ""), default=not left):
            review.finished = True
            click.echo("审核已结束。重新执行原来的 yakumi run 命令，从翻译继续。")


@terms.command("done")
@click.argument("job_id")
@click.pass_context
def terms_done(ctx, job_id):
    """结束审核：剩下待审核的术语不使用。之后重新执行 yakumi run 即从翻译继续。"""
    with _review_of(ctx, job_id) as (_job, review):
        review.finished = True
        left = review.summary()["pending"]
        click.echo("审核已结束" + (f"，{left} 个待审核的术语不会使用" if left else "") + "。")


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True,
              help="监听地址。部署到服务器、允许其他设备访问时用 0.0.0.0。")
@click.option("--port", default=8765, show_default=True, type=int)
@click.option("--no-auth", is_flag=True, help="对其他设备开放时也不要求登录（只在可信的局域网里使用）。")
@click.pass_context
def serve(ctx, host: str, port: int, no_auth: bool):
    """启动网页工作台。

    有 waitress 时使用它（pip install 'yakumi[server]'），否则使用 Flask 自带的服务器。
    """
    import logging
    import os

    from .web.app import create_app
    from .web.auth import PASSWORD_ENV

    web = _cfg(ctx)["web"]
    has_password = bool(os.environ.get(PASSWORD_ENV) or web.get("password_hash"))
    exposed = host not in LOOPBACK_HOSTS or int(web.get("proxy_count") or 0) > 0
    if exposed and not has_password and not no_auth:
        raise click.ClickException(
            "工作台将对其他设备开放，但还没有设置登录密码。\n"
            f"请先运行 yakumi passwd 设置密码（或设置环境变量 {PASSWORD_ENV}）；"
            "只在可信的局域网里使用时，可以加 --no-auth。")
    app = create_app(ctx.obj.get("config"))
    shown = f"[{host}]" if ":" in host else host
    click.echo(f"Yakumi 网页工作台：http://{shown}:{port}"
               + ("（所有网卡）" if host in ("0.0.0.0", "::") else "")
               + ("，需要登录" if has_password else ""), err=True)
    try:
        import waitress
    except ImportError:
        waitress = None
    if waitress is not None:
        max_mb = int(web.get("max_upload_mb", 4096) or 0)
        logging.getLogger("waitress").setLevel(logging.ERROR)
        waitress.serve(app, host=host, port=port, threads=8, ident="yakumi",
                       max_request_body_size=max_mb * 1024 * 1024 if max_mb > 0 else 1 << 50)
        return
    from werkzeug.serving import run_simple

    # Flask 自带的服务器启动时总会提示“这是开发用服务器”，并逐条打印请求；这里只保留错误。
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    if exposed:
        click.echo("提示：对其他设备开放时，建议安装 waitress：pip install 'yakumi[server]'", err=True)
    run_simple(host, port, app, threaded=True)


@main.command()
@click.option("--clear", is_flag=True, help="删除登录密码，之后访问工作台不需要登录。")
@click.pass_context
def passwd(ctx, clear: bool):
    """设置网页工作台的登录密码（部署到服务器、对其他设备开放时需要）。"""
    import os

    from . import settings as settings_store
    from .web.auth import PASSWORD_ENV, hash_password

    if clear:
        value = None
    else:
        password = click.prompt("新密码", hide_input=True, confirmation_prompt="再输入一次")
        if len(password) < 8:
            raise click.ClickException("密码至少需要 8 个字符")
        value = hash_password(password)
    try:
        path = settings_store.write_values(ctx.obj.get("config"), {"web.password_hash": value})
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"无法写入配置文件：{exc}") from exc
    click.echo(f"已{'删除' if clear else '保存'}登录密码（{path}）。重启 yakumi serve 后生效，已登录的设备需要重新登录。")
    if os.environ.get(PASSWORD_ENV):
        click.echo(click.style(f"! 当前设置了环境变量 {PASSWORD_ENV}，它优先于配置文件里的密码。", fg="yellow"), err=True)


@main.command()
@click.pass_context
def jobs(ctx):
    """列出最近的任务。"""
    cfg = _cfg(ctx)
    states = Job.list(cfg["workspace"]["dir"])
    if not states:
        click.echo("还没有任务。")
    for st in states[:20]:
        name = Path(st.video).name if st.video else "（导入的字幕）"
        click.echo(f"{st.id}  {st.status:<7} {st.target_lang or '-':<5} {name}")


@main.command()
@click.option("--online", is_flag=True, help="同时测试接口能否连通、密钥是否有效。")
@click.pass_context
def doctor(ctx, online: bool):
    """检查运行环境与配置。"""
    from .doctor import local_checks, online_checks

    cfg = _cfg(ctx)
    checks = local_checks(cfg, ctx.obj.get("config"))
    if online:
        checks += online_checks(cfg)
    marks = {"ok": click.style("✓", fg="green"), "warn": click.style("!", fg="yellow"),
             "fail": click.style("✗", fg="red"), "info": "•"}
    for check in checks:
        click.echo(f"{marks[check.status]} {check.title}")
        for line in check.detail + ([check.hint] if check.hint else []):
            click.echo(f"    {line}")
    sys.exit(1 if any(c.failed for c in checks) else 0)


if __name__ == "__main__":
    main()
