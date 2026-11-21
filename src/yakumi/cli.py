"""yakumi 命令行。"""

from __future__ import annotations

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
from .config import ConfigError, config_files, is_local_service, is_openrouter, load_config
from .models import STAGES, JobState
from .pipeline import STAGE_LABELS, Job, PipelineError, record_translation_usage, run, translation_config
from .segment import INHERITED, segment_llm_config
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
            raise click.ClickException(f"{exc}\n用 yakumi glossary list 查看已有的术语表。") from exc
    return list(ids)


# 只影响密钥、速度或客户端等待时间的设置，不改变阶段结果，不参与指纹。
# asr.timeout 会发给服务端并决定分窗，需要保留；translate.concurrency 决定上下文是否带译文，也要保留。
RUNTIME_ONLY = {
    "asr": {"api_key", "threads"},
    "segment": {"api_key", "timeout", "concurrency"},
    "translate": {"api_key", "timeout"},
}


def _stage_inputs(cfg: dict[str, Any], state: JobState) -> dict[str, str]:
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
        translation = translation_config(cfg["translate"], state.glossaries, state.notes)
        for key in RUNTIME_ONLY["translate"]:
            translation.pop(key, None)
        translation["glossary"] = list(translation["glossary"].items())
        inputs["translate"] = translation
    return {stage: hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
            for stage, value in inputs.items()}


def _update_job_inputs(job: Job, cfg: dict[str, Any], bilingual: bool, burn: bool, fmt: str,
                       glossaries: list[str] | None, notes: str | None) -> None:
    st = job.state
    st.bilingual, st.burn, st.format = bilingual, burn, fmt
    if glossaries is not None:
        st.glossaries = glossaries
    if notes is not None:
        st.notes = notes.strip()
    current = _stage_inputs(cfg, st)
    previous, st.stage_inputs = st.stage_inputs or {}, current
    changed = next((stage for stage in STAGES if previous.get(stage) != current.get(stage)), None)
    if previous and changed:
        job.invalidate_from(changed)
        click.echo(f"任务输入有变化，将从「{STAGE_LABELS[changed]}」重新处理", err=True)
    else:
        job.save()


@contextmanager
def _prepare_job(cfg: dict[str, Any], video: Path, target: str | None, source: str | None,
                 bilingual: bool, burn: bool, fmt: str, force: bool, glossaries: list[str] | None = None,
                 notes: str | None = None) -> Iterator[Job]:
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
            job_id = Job.create(workspace, video, target, source, bilingual, burn, fmt,
                                glossaries=glossaries, notes=notes or "", stage_inputs={}).state.id
        with Job.lock(workspace, job_id):
            job = Job.load(workspace, job_id, migrate=False)
            job.migrate()
            _update_job_inputs(job, cfg, bilingual, burn, fmt, glossaries, notes)
            if reuse:
                completed = "、".join(STAGE_LABELS[s] for s in job.state.completed) or "无"
                click.echo(f"沿用任务 {job_id}（已完成：{completed}）", err=True)
            yield job
    except (OSError, PipelineError) as exc:
        raise click.ClickException(str(exc)) from exc


def _execute(cfg: dict[str, Any], job: Job) -> None:
    try:
        run(job, cfg, progress=_progress_printer())
    except Exception as exc:
        raise click.ClickException(f"{exc}\n任务目录：{job.dir}\n重新执行同一命令即可从中断处继续。") from exc
    summary = format_stats(job.state.stats)
    if summary:
        click.echo(summary, err=True)


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
glossary_option = click.option("-g", "--glossary", "glossaries", multiple=True,
                               help="翻译时使用的术语表（可以重复指定多个），见 yakumi glossary。")
notes_option = click.option("--notes", default=None,
                            help="只对这个视频有效的补充信息，例如主播、节目、人物关系。")


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
@glossary_option
@notes_option
@asr_option
@model_option
@translator_option
@click.option("-o", "--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="输出目录，默认与视频同目录。")
@click.option("--force", is_flag=True, help="不复用之前的结果，从头开始。")
@click.pass_context
def run_cmd(ctx, video: Path, target, source, bilingual, burn, fmt, glossaries, notes, asr_provider, model,
            translate_provider, out_dir, force):
    """识别、翻译并导出字幕（可选烧录）。"""
    cfg = _cfg(ctx, asr={"provider": asr_provider},
               translate={"model": model, "provider": translate_provider})
    bilingual = cfg["subtitle"]["bilingual"] if bilingual is None else bilingual
    fmt = fmt or cfg["subtitle"]["format"]
    ids = _check_glossaries(glossaries) if glossaries else None
    with _prepare_job(cfg, video, target, source, bilingual, burn, fmt, force, ids, notes) as job:
        _execute(cfg, job)
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
@glossary_option
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
def glossary():
    """管理术语表（保存在 ~/.config/yakumi/glossaries/，可以在多个任务里复用）。"""


def _load_glossary(name: str) -> glossary_store.Glossary:
    try:
        return glossary_store.load(glossary_store.glossary_id(name))
    except glossary_store.GlossaryError as exc:
        raise click.ClickException(str(exc)) from exc


@glossary.command("list")
def glossary_list():
    """列出所有术语表。"""
    items = glossary_store.list_all()
    if not items:
        click.echo(f"还没有术语表。用 yakumi glossary set <名称> <原文> <译文> 创建，保存在 {glossary_store.glossaries_dir()}")
    for g in items:
        notes = g.notes.splitlines()[0][:30] if g.notes else ""
        click.echo(f"{g.id}  {len(g.terms)} 条术语" + (f"  {notes}" if notes else ""))


@glossary.command("show")
@click.argument("name")
def glossary_show(name):
    """显示术语表内容。"""
    g = _load_glossary(name)
    click.echo(f"# {g.name}（{glossary_store.path_of(g.id)}）")
    if g.notes:
        click.echo(f"\n补充信息：\n{g.notes}")
    click.echo(f"\n术语（{len(g.terms)} 条）：")
    for k, v in g.terms.items():
        click.echo(f"  {k} → {v or '（保留原文）'}")


def _get_or_new(name: str) -> glossary_store.Glossary:
    try:
        return glossary_store.load(glossary_store.glossary_id(name))
    except glossary_store.GlossaryError:
        try:
            return glossary_store.Glossary(name=glossary_store.glossary_id(name))
        except glossary_store.GlossaryError as exc:
            raise click.ClickException(str(exc)) from exc


@glossary.command("set")
@click.argument("name")
@click.argument("source")
@click.argument("target", default="")
def glossary_set(name, source, target):
    """添加或修改一条术语（术语表不存在时自动创建）。译文留空表示保留原文。"""
    g = _get_or_new(name)
    g.terms[source.strip()] = target.strip()
    glossary_store.save(g)
    click.echo(f"{g.id}：{source} → {target or '（保留原文）'}")


@glossary.command("unset")
@click.argument("name")
@click.argument("source")
def glossary_unset(name, source):
    """删除一条术语。"""
    g = _load_glossary(name)
    if g.terms.pop(source, None) is None:
        raise click.ClickException(f"{g.id} 里没有“{source}”")
    glossary_store.save(g)


@glossary.command("notes")
@click.argument("name")
@click.argument("text", required=False)
@click.option("-f", "--file", "file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="从文件读取补充信息。")
def glossary_notes(name, text, file):
    """设置术语表的补充信息（主播、节目、人物关系、口头禅等）。"""
    if file:
        text = file.read_text(encoding="utf-8")
    if text is None:
        raise click.ClickException("请提供补充信息文字，或用 -f 指定文件")
    g = _get_or_new(name)
    g.notes = text.strip()
    glossary_store.save(g)


@glossary.command("import")
@click.argument("name")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def glossary_import(name, file):
    """从 CSV / TSV / “原文 = 译文” 文本文件导入术语（一行一条，追加到术语表）。"""
    g = _get_or_new(name)
    terms = glossary_store.import_terms(file.read_text(encoding="utf-8-sig"))
    g.terms.update(terms)
    glossary_store.save(g)
    click.echo(f"{g.id}：导入 {len(terms)} 条，共 {len(g.terms)} 条")


@glossary.command("delete")
@click.argument("name")
@click.confirmation_option(prompt="确定删除这个术语表？")
def glossary_delete(name):
    """删除术语表。"""
    try:
        glossary_store.delete(glossary_store.glossary_id(name))
    except glossary_store.GlossaryError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
@click.pass_context
def serve(ctx, host: str, port: int):
    """启动网页工作台。"""
    from .web.app import create_app

    app = create_app(ctx.obj.get("config"))
    click.echo(f"Yakumi 网页工作台：http://{host}:{port}", err=True)
    app.run(host=host, port=port, threaded=True, debug=False)


@main.command()
@click.pass_context
def jobs(ctx):
    """列出最近的任务。"""
    cfg = _cfg(ctx)
    states = Job.list(cfg["workspace"]["dir"])
    if not states:
        click.echo("还没有任务。")
    for st in states[:20]:
        click.echo(f"{st.id}  {st.status:<7} {st.target_lang or '-':<5} {Path(st.video).name}")


@main.command()
@click.option("--online", is_flag=True, help="同时测试接口能否连通、密钥是否有效。")
@click.pass_context
def doctor(ctx, online: bool):
    """检查运行环境与配置。"""
    cfg = _cfg(ctx)
    ok = True

    def line(passed: bool, text: str, hint: str = "", warn: bool = False) -> None:
        nonlocal ok
        if not passed and not warn:
            ok = False
        mark = click.style("✓", fg="green") if passed else click.style("!" if warn else "✗",
                                                                       fg="yellow" if warn else "red")
        click.echo(f"{mark} {text}" + (f"\n    {hint}" if hint and not passed else ""))

    line(bool(shutil.which("ffmpeg")), "ffmpeg", "请安装 ffmpeg（Termux：pkg install ffmpeg）")
    line(bool(shutil.which("ffprobe")), "ffprobe")
    line(media.ffmpeg_has_libass(), "ffmpeg 支持字幕烧录（libass）", "烧录字幕需要带 libass 的 ffmpeg")

    found = config_files(ctx.obj.get("config"))
    click.echo(f"• 配置文件：{', '.join(map(str, found)) if found else '未找到，使用默认值和环境变量'}")

    # VAD
    from .chunking.vad import SILERO_VERSION, model_cache_dir, silero_available

    vad = cfg["chunking"].get("vad", "silero")
    if vad == "silero":
        if silero_available():
            cached = (model_cache_dir() / f"silero_vad_{SILERO_VERSION}.onnx").is_file()
            line(True, f"VAD：Silero {SILERO_VERSION}" + ("" if cached else "（模型将在首次使用时下载，约 2MB）"))
        else:
            line(False, "VAD：没有 onnxruntime，将改用能量检测（背景音乐较强时切点可能不准）",
                 "Termux：pkg install python-onnxruntime；其他平台：pip install 'yakumi[vad]'", warn=True)
    else:
        click.echo(f"• VAD：{vad}")

    asr = cfg["asr"]
    click.echo(f"• 语音识别：{asr['provider']}")
    if asr["provider"] == "whisper_cpp":
        binary = Path(asr["whisper_cpp_bin"]).expanduser()
        line(bool(shutil.which(str(binary)) or binary.is_file()), f"whisper.cpp 程序：{binary}")
        model = Path(asr["whisper_cpp_model"]).expanduser() if asr["whisper_cpp_model"] else None
        line(bool(model and model.is_file()), f"whisper.cpp 模型：{model or '(未配置)'}")
    else:
        click.echo(f"    {asr['base_url']}  模型 {asr['model']}")
        click.echo(f"    窗口 {asr['target_window']} 秒（上限 {asr['max_window']} 秒），上传格式 {asr.get('upload_format', 'flac')}")
        line(bool(asr.get("api_key")), "语音识别 API Key",
             "设置 OPENROUTER_API_KEY（或 YAKUMI_ASR_API_KEY），或写入配置文件")
        from .asr import create_asr
        from .chunking.transcribe import capability_cache_path

        cache = capability_cache_path()
        key = create_asr(asr).cache_key  # type: ignore[attr-defined]  # 只有 OpenAI 兼容后端有探测缓存
        if cache.is_file():
            import json

            entry = json.loads(cache.read_text(encoding="utf-8")).get(key)
            if entry:
                mode = {"words": "词级时间戳", "text": "纯文本"}.get(entry["mode"], entry["mode"])
                click.echo(f"    识别能力：{mode}（{entry.get('checked_at', '')} 探测）")

    tr = cfg["translate"]
    click.echo(f"• 翻译：{tr['base_url']}  模型 {tr['model']}")
    openrouter = is_openrouter(tr["base_url"])
    unit = "整句翻译" if tr.get("unit", "sentence") == "sentence" else "逐条翻译"
    click.echo(f"    {unit}，思考档位 {tr.get('reasoning_effort') or '模型默认'}，并发 {tr.get('concurrency', 1)}"
               + (f"，路由 {tr.get('routing', 'default')}" if openrouter else ""))
    local = is_local_service(tr["base_url"])
    line(bool(tr.get("api_key")) or local, "翻译 API Key",
         "设置 OPENROUTER_API_KEY（或 YAKUMI_TRANSLATE_API_KEY）；本地服务可以不填")

    from .segment import segment_llm_config

    sg = cfg.get("segment", {})
    seg_cfg = segment_llm_config(cfg)
    if sg.get("mode", "llm") == "llm":
        click.echo(f"• 断句：LLM，模型 {seg_cfg['model']}，思考档位 {seg_cfg.get('reasoning_effort') or '模型默认'}")
        seg_local = is_local_service(seg_cfg["base_url"])
        line(bool(seg_cfg.get("api_key")) or seg_local, "断句可以调用模型",
             "没有密钥，将改用规则断句（只按句末标点）", warn=True)
    else:
        click.echo("• 断句：规则（只按句末标点）")
    click.echo(f"• 字幕标点：{'按字幕惯例（中日文句中逗号句号换成空格，行尾的去掉）' if cfg['subtitle'].get('punctuation', 'subtitle') == 'subtitle' else '原样保留'}")

    if online:
        import httpx

        targets = [("翻译接口", tr["base_url"], tr.get("api_key"))]
        if asr["provider"] == "openai":
            targets.insert(0, ("语音识别接口", asr["base_url"], asr.get("api_key")))
        for name, base, key in targets:
            # OpenRouter 的 /key 能同时验证密钥；其他兼容服务用 /models
            path = "/key" if is_openrouter(base) else "/models"
            try:
                resp = httpx.get(base.rstrip("/") + path, timeout=15,
                                 headers={"Authorization": f"Bearer {key}"} if key else {})
                line(resp.status_code < 400, f"{name}连通，密钥有效（HTTP {resp.status_code}）",
                     resp.text[:200])
            except httpx.HTTPError as exc:
                line(False, f"{name}连通", str(exc))
        if openrouter:
            _check_openrouter_model(tr, line, "翻译", "translate.reasoning_effort")
            if sg.get("mode", "llm") == "llm" and is_openrouter(seg_cfg["base_url"]):
                _check_openrouter_model(seg_cfg, line, "断句", "segment.reasoning_effort")
    sys.exit(0 if ok else 1)


def _check_openrouter_model(tr: dict[str, Any], line, name: str = "翻译",
                            effort_key: str = "translate.reasoning_effort") -> None:
    """用 OpenRouter 的模型列表核对模型是否存在、思考档位是否受支持。"""
    import httpx

    base_model = tr["model"].split(":", 1)[0]
    try:
        resp = httpx.get(tr["base_url"].rstrip("/") + "/models", timeout=20)
        models = {m["id"]: m for m in resp.json().get("data", [])}
    except (httpx.HTTPError, ValueError) as exc:
        line(False, "读取 OpenRouter 模型列表", str(exc), warn=True)
        return
    model = models.get(base_model)
    if not model:
        line(False, f"{name}模型 {base_model} 在 OpenRouter 上存在", "请检查模型名")
        return
    reasoning = model.get("reasoning") or {}
    efforts = reasoning.get("supported_efforts") or []
    effort = (tr.get("reasoning_effort") or "").lower()
    desc = f"支持的思考档位 {', '.join(efforts) or '无'}" + ("（不能关闭）" if reasoning.get("mandatory") else "")
    if not effort:
        line(True, f"{name}模型存在，{desc}")
    elif effort == "none":
        line(not reasoning.get("mandatory"), f"{name}思考档位 none，{desc}",
             "该模型不能关闭思考，请改为 " + " / ".join(efforts))
    elif efforts:
        line(effort in efforts, f"{name}思考档位 {effort}，{desc}", f"请把 {effort_key} 改为 " + " / ".join(efforts))
    else:
        line(False, f"{name}思考档位 {effort}，但该模型没有声明支持的档位", "可能会被忽略或报错", warn=True)


if __name__ == "__main__":
    main()
