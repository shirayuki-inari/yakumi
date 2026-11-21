"""基于 OpenAI 兼容对话接口的翻译。请求、重试、思考档位和路由见 yakumi.llm。"""

from __future__ import annotations

from typing import Any

import httpx

from ..llm import ROUTING_SUFFIX, ChatClient
from .base import TranslateError, Translator

__all__ = ["OpenAICompatTranslator", "ROUTING_SUFFIX"]


class OpenAICompatTranslator(Translator):
    def __init__(self, base_url: str, model: str, api_key: str = "", temperature: float | None = 0.2,
                 timeout: float = 180.0, retries: int = 3, client: httpx.Client | None = None,
                 reasoning_effort: str = "", routing: str = "default",
                 provider: dict[str, Any] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.chat = ChatClient(base_url, model, api_key, temperature=temperature, timeout=timeout,
                               retries=retries, client=client, reasoning_effort=reasoning_effort,
                               routing=routing, provider=provider, error=TranslateError,
                               effort_key="translate.reasoning_effort", label="翻译")

    @property
    def usage(self) -> dict[str, float]:
        return self.chat.usage

    @property
    def request_model(self) -> str:
        return self.chat.request_model

    def payload(self, system: str, user: str) -> dict[str, Any]:
        return self.chat.payload(system, user)

    def complete(self, system: str, user: str) -> str:
        return self.chat.complete(system, user)
