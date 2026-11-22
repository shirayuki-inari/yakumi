import json
import subprocess

from yakumi import media


def test_probe_extract_and_burn_apply_local_input_policy(tmp_path, monkeypatch):
    source = tmp_path / "input.mp4"
    subtitles = tmp_path / "subtitles.srt"
    subtitles.touch()
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        streams = [
            {"index": 1, "codec_name": "aac", "channels": 2, "disposition": {"default": 0}},
            {"index": 2, "codec_name": "pcm_s16le", "channels": 1, "disposition": {"default": 1}},
        ]
        return subprocess.CompletedProcess(args, 0, json.dumps({"streams": streams}), "")

    monkeypatch.setattr(media, "_bin", lambda name: name)
    monkeypatch.setattr(media, "_run", run)
    media.extract_audio(source, tmp_path / "audio.wav")
    media.burn_subtitles(source, subtitles, tmp_path / "output.mp4")
    assert len(commands) == 4
    for args in commands:
        assert args[args.index("-protocol_whitelist") + 1] == "file"
        allowed = args[args.index("-format_whitelist") + 1].split(",")
        assert "mov" in allowed and not {"hls", "concat", "dash"}.intersection(allowed)
        assert str(source.resolve()) in args
    assert "0:2" in commands[1] and "0:2" in commands[3]
    assert commands[3][commands[3].index("-c:a") + 1] == "aac"
