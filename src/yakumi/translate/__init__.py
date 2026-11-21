from __future__ import annotations

from typing import Any

from .base import TranslateError, Translator, lang_name


def create_translator(cfg: dict[str, Any], target: str = "zh") -> Translator:
    common = {
        "batch_size": int(cfg.get("batch_size", 30)),
        "style": cfg.get("style", ""),
        "glossary": cfg.get("glossary") or {},
        "concurrency": int(cfg.get("concurrency", 1)),
        "notes": str(cfg.get("notes", "") or ""),
        "source_kind": str(cfg.get("source_kind", "asr")),
    }
    provider = cfg.get("provider", "openai")
    if provider == "openai":
        from .openai_compat import OpenAICompatTranslator

        temperature = cfg.get("temperature", 0.2)
        return OpenAICompatTranslator(
            cfg["base_url"], cfg["model"], cfg.get("api_key", ""),
            temperature=None if temperature in (None, "") else float(temperature),
            timeout=float(cfg.get("timeout", 180)),
            reasoning_effort=str(cfg.get("reasoning_effort", "") or ""),
            routing=str(cfg.get("routing", "default") or "default"),
            provider=cfg.get("provider_routing") or None,
            **common,
        )
    if provider == "fake":
        from .fake import FakeTranslator

        return FakeTranslator(target=target, **common)
    raise TranslateError(f"未知的翻译方式：{provider}")


__all__ = ["TranslateError", "Translator", "create_translator", "lang_name"]
