from __future__ import annotations

from typing import Any

from .base import ASRError, ASRProvider, ASRTimeout, ASRTooLarge
from .types import ASRCapabilities, AudioWindow, Boundary, RawTranscript, Word

PROVIDERS = ("openai", "whisper_cpp")


def create_asr(cfg: dict[str, Any]) -> ASRProvider:
    provider = cfg.get("provider", "openai")
    if provider == "openai":
        from .openai_compat import OpenAICompatASR

        return OpenAICompatASR(
            cfg["base_url"], cfg["model"], cfg.get("api_key", ""),
            mode=cfg.get("mode", "auto"), timeout=float(cfg.get("timeout", 60)),
            target_window=float(cfg.get("target_window", 300)), max_window=float(cfg.get("max_window", 480)),
        )
    if provider == "whisper_cpp":
        from .whisper_cpp import WhisperCppASR

        return WhisperCppASR(cfg["whisper_cpp_bin"], cfg["whisper_cpp_model"], int(cfg.get("threads", 4)))
    if provider == "fake":
        from .fake import FakeASR

        mode = cfg.get("mode") if cfg.get("mode") in ("words", "text") else "words"
        return FakeASR(mode=mode,
                       target_window=float(cfg.get("target_window", 300)),
                       max_window=float(cfg.get("max_window", 480)),
                       timeout_over=float(cfg.get("fake_timeout_over", float("inf"))))
    raise ASRError(f"未知的语音识别方式：{provider}")


__all__ = [
    "ASRCapabilities", "ASRError", "ASRProvider", "ASRTimeout", "ASRTooLarge", "AudioWindow",
    "Boundary", "PROVIDERS", "RawTranscript", "Word", "create_asr",
]
