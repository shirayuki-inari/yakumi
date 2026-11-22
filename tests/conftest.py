import shutil
import subprocess

import pytest

from yakumi.config import load_config

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg")


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """测试不读写真实的 ~/.cache/yakumi（能力探测缓存等）、~/.config/yakumi（背景资料等）
    和 ~/.local/share/yakumi（默认任务目录）。"""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("YAKUMI_WORKSPACE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("YAKUMI_WEB_PASSWORD", raising=False)


@pytest.fixture
def fake_cfg(tmp_path):
    return load_config(
        path=tmp_path / "none.toml",
        use_env=False,
        overrides={"asr": {"provider": "fake"}, "translate": {"provider": "fake"},
                   "chunking": {"vad": "energy"},
                   "workspace": {"dir": str(tmp_path / "ws")}},
    )


@pytest.fixture
def make_video(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("需要 ffmpeg")

    def make(seconds: float = 5, name: str = "sample.mp4"):
        out = tmp_path / name
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
             "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds), "-c:v", "libx264",
             "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out)],
            check=True,
        )
        return out

    return make


@pytest.fixture
def sample_video(make_video):
    return make_video(5)
