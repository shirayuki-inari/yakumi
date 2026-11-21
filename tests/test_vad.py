import shutil
import subprocess

import pytest

from yakumi.chunking import vad as vad_mod
from yakumi.chunking.vad import (EnergyVAD, NoVAD, SileroVAD, complement, create_vad, model_cache_dir,
                                 parse_silencedetect, regions_from_probs, silero_available)


def test_regions_from_probs_hysteresis():
    f = 0.1
    probs = [0.1, 0.6, 0.45, 0.4, 0.2, 0.7, 0.1, 0.1, 0.1, 0.9, 0.9]
    # 0.45/0.4 高于退出阈值 0.35，不算静音；单帧 0.2 的静音短于 min_silence，也不切开
    assert regions_from_probs(probs, f, 0.5, 0.35, min_silence=0.15) == [(0.1, 0.6), (0.9, 1.1)]


def test_short_blip_is_kept():
    # 只有一帧的短促语音也保留（只用于挑切点，不能丢）
    assert regions_from_probs([0.0, 0.9, 0.0, 0.0, 0.0], 0.1, 0.5, 0.35, 0.1) == [(0.1, 0.2)]


def test_create_vad_falls_back_without_onnxruntime(monkeypatch, capsys):
    monkeypatch.setattr(vad_mod, "silero_available", lambda: False)
    assert isinstance(create_vad({"vad": "silero"}), EnergyVAD)
    assert "onnxruntime" in capsys.readouterr().err
    assert isinstance(create_vad({"vad": "none"}), NoVAD)


@pytest.mark.skipif(not (silero_available() and shutil.which("ffmpeg")
                         and (model_cache_dir() / f"silero_vad_{vad_mod.SILERO_VERSION}.onnx").is_file()),
                    reason="需要 onnxruntime 和已缓存的 Silero 模型")
def test_silero_runs_and_ignores_pure_tone(tmp_path):
    out = tmp_path / "tone.flac"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=3",
                    "-ar", "16000", "-ac", "1", str(out)], check=True)
    v = SileroVAD()
    probs = v.probabilities(out)
    assert len(probs) == pytest.approx(3 * 16000 / 512, abs=2)
    assert all(0.0 <= p <= 1.0 for p in probs)
    assert v.speech_regions(out) == []  # 纯音调不是语音


def test_parse_silencedetect_and_complement():
    log = (
        "[silencedetect @ 0x1] silence_start: 0\n"
        "[silencedetect @ 0x1] silence_end: 1.5 | silence_duration: 1.5\n"
        "[silencedetect @ 0x1] silence_start: 4.25\n"
        "[silencedetect @ 0x1] silence_end: 5 | silence_duration: 0.75\n"
        "[silencedetect @ 0x1] silence_start: 9\n"
    )
    silences = parse_silencedetect(log, 10.0)
    assert silences == [(0.0, 1.5), (4.25, 5.0), (9.0, 10.0)]
    assert complement(silences, 10.0) == [(1.5, 4.25), (5.0, 9.0)]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg")
def test_energy_vad_on_tone_and_silence(tmp_path):
    # 1 秒静音，2 秒音调，1 秒静音，2 秒音调，1 秒静音
    out = tmp_path / "t.flac"
    tone = "sine=frequency=300:sample_rate=16000:duration=2"
    gap = "anullsrc=r=16000:cl=mono"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-t", "1", "-i", gap, "-f", "lavfi", "-i", tone,
         "-f", "lavfi", "-t", "1", "-i", gap, "-f", "lavfi", "-i", tone,
         "-f", "lavfi", "-t", "1", "-i", gap,
         "-filter_complex", "[0][1][2][3][4]concat=n=5:v=0:a=1", "-ac", "1", str(out)],
        check=True,
    )
    regions = EnergyVAD().speech_regions(out)
    assert len(regions) == 2
    (s1, e1), (s2, e2) = regions
    assert s1 == pytest.approx(1, abs=0.15) and e1 == pytest.approx(3, abs=0.15)
    assert s2 == pytest.approx(4, abs=0.15) and e2 == pytest.approx(6, abs=0.15)
