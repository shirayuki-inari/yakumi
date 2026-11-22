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
from .config import ConfigError, load_config
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
