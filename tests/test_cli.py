import json
import stat
import sys

import pytest
from click.testing import CliRunner

from yakumi import media
from yakumi.asr.base import ASRError
from yakumi.asr.whisper_cpp import WhisperCppASR
from yakumi.cli import main
from yakumi.config import ConfigError, load_config, unknown_keys
from yakumi.pipeline import Job, PipelineError


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """在临时目录里运行命令行，使用假识别和假翻译。"""
    monkeypatch.chdir(tmp_path)
    for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "yakumi.toml").write_text('[chunking]\nvad = "energy"\n', encoding="utf-8")
    runner = CliRunner()

    def invoke(*args):
        return runner.invoke(main, list(args), catch_exceptions=False)

    return invoke


def test_run_then_reuse_and_reexport(cli, sample_video, tmp_path):
    out = tmp_path / "out"
    res = cli("run", str(sample_video), "-t", "zh", "--asr", "fake", "--translator", "fake", "-o", str(out))
    assert res.exit_code == 0, res.output
    srt = out / "sample.zh.srt"
    assert srt.is_file() and "[zh]" in srt.read_text(encoding="utf-8")
    assert "识别：" in res.output and "费用" in res.output
    # 再次执行沿用任务；只改导出格式时只重新生成字幕
    res = cli("run", str(sample_video), "-t", "zh", "--asr", "fake", "--translator", "fake", "-o", str(out),
              "--format", "vtt")
    assert "沿用任务" in res.output and (out / "sample.zh.vtt").is_file()
    states = Job.list(tmp_path / ".yakumi")
    assert len(states) == 1 and states[0].format == "vtt"


def test_transcribe_only(cli, sample_video, tmp_path):
    res = cli("transcribe", str(sample_video), "--asr", "fake", "-o", str(tmp_path / "o"))
    assert res.exit_code == 0, res.output
    assert "w0" in (tmp_path / "o" / "sample.orig.srt").read_text(encoding="utf-8")


def test_translate_existing_subtitles(cli, tmp_path):
    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n音が\n\n2\n00:00:01,000 --> 00:00:02,000\n微妙かな。\n",
                   encoding="utf-8")
    res = cli("translate", str(srt), "-t", "zh", "--translator", "fake", "--bilingual")
    assert res.exit_code == 0, res.output
    text = (tmp_path / "a.zh.srt").read_text(encoding="utf-8")
    # 两条按句末标点并成一句整句翻译；导出时按字幕惯例去掉行尾句号
    assert "[zh] 音が\n音が" in text and "[zh] 微妙かな\n微妙かな" in text


def test_missing_and_broken_config(cli, tmp_path):
    assert "does not exist" in cli("-c", "nope.toml", "jobs").output
    bad = tmp_path / "bad.toml"
    bad.write_text("[asr\n", encoding="utf-8")
    res = cli("-c", str(bad), "jobs")
    assert res.exit_code != 0 and "格式有误" in res.output
    typo = tmp_path / "typo.toml"
    typo.write_text('[translate]\nmodle = "x"\n[translate.glossary]\n"任意" = "键"\n[segment]\nmodel = "m"\n',
                    encoding="utf-8")
    res = cli("-c", str(typo), "jobs")
    assert res.exit_code == 0 and "translate.modle" in res.output and "任意" not in res.output


def test_unknown_keys_and_config_error(tmp_path):
    assert unknown_keys({"asr": {"modle": 1}, "nope": {}, "translate": {"glossary": {"x": "y"}}}) == \
        ["asr.modle", "nope"]
    bad = tmp_path / "b.toml"
    bad.write_text("x = [", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_doctor_offline(cli):
    res = cli("doctor")
    assert "ffmpeg" in res.output and "断句" in res.output


def test_python_m_entry_point():
    import subprocess

    proc = subprocess.run([sys.executable, "-m", "yakumi", "--version"], capture_output=True, text=True)
    assert proc.returncode == 0 and "yakumi" in proc.stdout


def test_job_id_validation(tmp_path):
    for bad in ("../x", "a/b", ".hidden", ""):
        with pytest.raises(PipelineError):
            Job.load(tmp_path, bad)


# ---- whisper.cpp（用假的可执行文件模拟）------------------------------------
def _fake_whisper(tmp_path, body: str):
    script = tmp_path / "whisper-cli"
    script.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do if [ \"$1\" = -of ]; then out=\"$2\"; fi; shift; done\n"
        f"cat > \"$out.json\" <<'EOF'\n{body}\nEOF\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    model = tmp_path / "model.bin"
    model.write_bytes(b"x")
    return WhisperCppASR(str(script), str(model))


def test_whisper_cpp_parses_segments(tmp_path, sample_video):
    body = json.dumps({"result": {"language": "ja"}, "transcription": [
        {"offsets": {"from": 0, "to": 1500}, "text": " こんにちは"}, {"offsets": {"from": 1500, "to": 3000}, "text": ""}]})
    asr = _fake_whisper(tmp_path, body)
    assert asr.mode == "text"
    raw = asr.transcribe(sample_video)
    assert raw.language == "ja" and [(s.start, s.end, s.text) for s in raw.segments] == [(0.0, 1.5, "こんにちは")]


def test_whisper_cpp_bad_json_is_asr_error(tmp_path, sample_video):
    with pytest.raises(ASRError, match="JSON"):
        _fake_whisper(tmp_path, "{not json").transcribe(sample_video)


def test_upload_suffix_and_audio_codec(sample_video):
    assert media.upload_suffix("opus") == ".ogg"
    with pytest.raises(media.MediaError):
        media.upload_suffix("wav")
    assert media.audio_codec(sample_video) == "aac"


def test_example_config_matches_defaults():
    """yakumi.example.toml 列出的值必须与内置默认值一致，且没有写错位置的键。"""
    import tomllib
    from pathlib import Path

    from yakumi.config import DEFAULTS

    data = tomllib.loads((Path(__file__).parent.parent / "yakumi.example.toml").read_text(encoding="utf-8"))
    assert unknown_keys(data) == []
    for section, values in data.items():
        for key, value in values.items():
            if not isinstance(value, dict):
                assert DEFAULTS[section][key] == value, f"{section}.{key}"
    assert data["translate"]["provider_routing"] == {} and data["translate"]["glossary"] == {}
