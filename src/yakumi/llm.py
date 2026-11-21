"""OpenAI 兼容的对话接口（POST /chat/completions），翻译和断句共用。

云端服务（OpenRouter、OpenAI、DeepSeek 等）和本地服务（Ollama、llama.cpp server、vLLM 等）
都可以使用，只需修改 base_url 和 model。

思考档位（reasoning_effort）：
- 指向 OpenRouter 时发送 `reasoning: {"effort": ..., "exclude": true}`。exclude 让响应里不带思考过程
  （用不到）；思考 token 仍按输出价格计费，也计入 max_tokens，所以这里不设 max_tokens。
- 其他服务发送标准的顶层 `reasoning_effort`。
- 留空则不发送，由模型使用默认档位。有些模型的思考不能关闭（例如 google/gemini-3.8-flash，
  可选 low / medium / high），传入不支持的值时接口会返回 400，这里会给出明确提示。

路由模式（routing，仅 OpenRouter）：
- default：OpenRouter 默认路由（综合价格、延迟、可用性挑选 provider）
- floor：按价格排序，并允许使用更便宜的 flex 服务档（不保证一定命中 flex）
- nitro：按吞吐排序，并允许使用更贵的 priority 服务档
另外可以用 provider 参数做更细的控制（order / only / ignore / sort / allow_fallbacks 等），
作为请求里的 provider 字段原样透传。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import httpx

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}
ROUTING_SUFFIX = {"default": "", "floor": ":floor", "nitro": ":nitro"}
THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class LLMError(RuntimeError):
    pass


def new_usage() -> dict[str, float]:
    return {"requests": 0, "failed": 0, "cost": 0.0, "cost_unknown": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}


class ChatClient:
    """一个模型的对话客户端：请求、重试、思考档位、路由和用量统计（线程安全）。"""

    def __init__(self, base_url: str, model: str, api_key: str = "", temperature: float | None = 0.2,
                 timeout: float = 180.0, retries: int = 3, client: httpx.Client | None = None,
                 reasoning_effort: str = "", routing: str = "default",
                 provider: dict[str, Any] | None = None, error: type[Exception] = LLMError,
                 effort_key: str = "reasoning_effort", label: str = "模型"):
        self.error = error
        if routing not in ROUTING_SUFFIX:
            raise error(f"未知的路由模式：{routing}（可选 default / floor / nitro）")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.openrouter = "openrouter.ai" in base_url
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.retries = retries
        self.reasoning_effort = (reasoning_effort or "").strip().lower()
        self.routing = routing
        self.provider = provider or {}
        self.effort_key = effort_key  # 出错提示里指向的配置项
        self.label = label
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=15))
        self.usage = new_usage()
        self._lock = threading.Lock()

    @property
    def request_model(self) -> str:
        if self.openrouter and ":" not in self.model:
            return self.model + ROUTING_SUFFIX[self.routing]
        return self.model

    def payload(self, system: str, user: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.request_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.reasoning_effort:
            if self.openrouter:
                if self.reasoning_effort == "none":
                    body["reasoning"] = {"enabled": False}
                else:
                    body["reasoning"] = {"effort": self.reasoning_effort, "exclude": True}
            else:
                body["reasoning_effort"] = self.reasoning_effort
        if self.openrouter and self.provider:
            body["provider"] = self.provider
        return body

    def _record(self, data: dict[str, Any] | None, failed: bool = False) -> None:
        with self._lock:
            self.usage["requests"] += 1
            if failed or data is None:
                self.usage["failed"] += 1
                return
            usage = data.get("usage") or {}
            if usage.get("cost") is None:
                self.usage["cost_unknown"] += 1
            else:
                self.usage["cost"] += float(usage["cost"])
            self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            details = usage.get("completion_tokens_details") or {}
            self.usage["reasoning_tokens"] += int(details.get("reasoning_tokens") or 0)

    def complete(self, system: str, user: str) -> str:
        payload = self.payload(system, user)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last_error = ""
        for attempt in range(self.retries):
            try:
                resp = self.client.post(self.url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_error = str(exc)
            else:
                if resp.status_code < 400:
                    try:
                        data = resp.json()
                        content = data["choices"][0]["message"]["content"] or ""
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        self._record(None, failed=True)
                        raise self.error(f"{self.label}接口返回格式异常：{resp.text[:300]}") from exc
                    self._record(data)
                    return THINK_RE.sub("", content)
                last_error = f"HTTP {resp.status_code}：{resp.text[:300]}"
                if resp.status_code == 400 and "reasoning" in resp.text.lower():
                    self._record(None, failed=True)
                    raise self.error(
                        f"模型不接受当前的思考档位设置（reasoning_effort = {self.reasoning_effort or '未设置'}）："
                        f"{resp.text[:300]}\n请修改配置中的 {self.effort_key}。")
                if resp.status_code not in RETRY_STATUS:
                    break
            if attempt + 1 < self.retries:
                time.sleep(2 ** attempt)
        self._record(None, failed=True)
        raise self.error(f"{self.label}请求失败：{last_error}")


def chat_from_config(cfg: dict[str, Any], **kwargs: Any) -> ChatClient:
    """按配置段（[translate] 这类，含 base_url / model / api_key 等）创建客户端。"""
    temperature = cfg.get("temperature", 0.2)
    return ChatClient(
        cfg["base_url"], cfg["model"], cfg.get("api_key", ""),
        temperature=None if temperature in (None, "") else float(temperature),
        timeout=float(cfg.get("timeout", 180)),
        reasoning_effort=str(cfg.get("reasoning_effort", "") or ""),
        routing=str(cfg.get("routing", "default") or "default"),
        provider=cfg.get("provider_routing") or None,
        **kwargs,
    )


def record_usage(stats: dict[str, Any], prefix: str, usage: dict[str, float] | None) -> None:
    """把一次运行的用量累加进任务统计（费用跨运行累计，因为钱确实花了）。"""
    if not usage:
        return
    for key in ("requests", "failed", "cost", "cost_unknown", "reasoning_tokens"):
        stats[f"{prefix}_{key}"] = round(stats.get(f"{prefix}_{key}", 0) + usage.get(key, 0), 6)
