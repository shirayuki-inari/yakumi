"""OpenAI 兼容的语音识别接口（POST /audio/transcriptions）。

默认指向 OpenRouter，也适用于 OpenAI、Groq 等实现了同一接口的服务。
优先请求 `verbose_json` + 词级时间戳；后端不支持时（返回 400）自动降级为纯文本。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from ..models import Segment
from .base import ASRError, ASRTimeout, ASRTooLarge
from .types import ASRCapabilities, AudioWindow, RawTranscript, Word

RETRY_STATUS = {429, 500, 502, 503}
TIMEOUT_STATUS = {408, 504, 524}
MIME = {".flac": "audio/flac", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".ogg": "audio/ogg", ".opus": "audio/ogg", ".webm": "audio/webm"}


class OpenAICompatASR:
    name = "openai"

    def __init__(self, base_url: str, model: str, api_key: str = "", *, mode: str = "auto",
                 timeout: float = 60.0, target_window: float = 300.0, max_window: float = 480.0,
                 retries: int = 2, client: httpx.Client | None = None):
        if mode not in ("auto", "words", "text"):
            raise ASRError(f"未知的识别模式：{mode}")
        self.url = base_url.rstrip("/") + "/audio/transcriptions"
        self.model = model
        self.api_key = api_key
        self.mode = mode
        self.server_timeout = timeout
        self.target_window = target_window
        self.max_window = max_window
        self.retries = retries
        # 客户端超时比服务端多留余量，让服务端的超时错误先返回
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout + 30, connect=15))

    @property
    def cache_key(self) -> str:
        """能力探测结果的缓存键。"""
        return f"{self.url}|{self.model}"

    def capabilities(self) -> ASRCapabilities:
        return ASRCapabilities(
            word_timestamps=self.mode != "text",
            max_window=self.max_window,
            target_window=self.target_window,
            timeout=self.server_timeout,
        )

    # ---- 请求 ------------------------------------------------------------
    def _form(self, language: str | None, verbose: bool) -> dict[str, Any]:
        data: dict[str, Any] = {"model": self.model, "response_format": "verbose_json" if verbose else "json"}
        if verbose:
            data["timestamp_granularities[]"] = ["word", "segment"]
        if language:
            data["language"] = language.split("-")[0].lower()
        return data

    def _post(self, clip: Path, data: dict[str, Any]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last = ""
        for attempt in range(self.retries + 1):
            try:
                with open(clip, "rb") as f:
                    files = {"file": (clip.name, f, MIME.get(clip.suffix.lower(), "application/octet-stream"))}
                    resp = self.client.post(self.url, data=data, files=files, headers=headers)
            except httpx.TimeoutException as exc:
                raise ASRTimeout(f"语音识别请求超时：{exc}") from exc
            except httpx.HTTPError as exc:
                last = f"语音识别请求失败：{exc}"
            else:
                if resp.status_code in TIMEOUT_STATUS or _mentions_timeout(resp):
                    raise ASRTimeout(f"语音识别超时（HTTP {resp.status_code}）：{resp.text[:200]}")
                if resp.status_code == 413 or (resp.status_code == 400 and "exceeds" in resp.text.lower()):
                    raise ASRTooLarge(f"音频片段过大：{resp.text[:200]}")
                if resp.status_code not in RETRY_STATUS:
                    return resp
                last = f"语音识别接口返回 {resp.status_code}：{resp.text[:200]}"
            if attempt < self.retries:
                time.sleep(2 ** attempt)
        raise ASRError(last)

    def transcribe(self, clip: Path, language: str | None = None,
                   window: AudioWindow | None = None) -> RawTranscript:
        t0 = time.monotonic()
        verbose = self.mode != "text"
        resp = self._post(clip, self._form(language, verbose))
        if verbose and resp.status_code == 400 and _rejects_verbose(resp):
            if self.mode == "words":
                raise ASRError(f"该模型不支持词级时间戳：{resp.text[:200]}")
            self.mode = "text"  # 自动降级，后续请求直接用纯文本
            resp = self._post(clip, self._form(language, False))
        if resp.status_code >= 400:
            raise ASRError(f"语音识别接口返回 {resp.status_code}：{resp.text[:300]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ASRError("语音识别接口返回的不是 JSON") from exc
        result = parse_response(body)
        result.elapsed = time.monotonic() - t0
        return result


def _mentions_timeout(resp: httpx.Response) -> bool:
    return resp.status_code >= 400 and "timed out" in resp.text.lower()


def _rejects_verbose(resp: httpx.Response) -> bool:
    text = resp.text.lower()
    return "verbose_json" in text or "response_format" in text or "timestamp" in text


def parse_response(body: dict[str, Any]) -> RawTranscript:
    words = []
    for w in body.get("words") or []:
        text = str(w.get("word", w.get("text", ""))).strip()
        if text:
            words.append(Word(float(w["start"]), float(w["end"]), text, w.get("confidence")))
    segments = [
        Segment(float(s["start"]), float(s["end"]), str(s.get("text", "")).strip())
        for s in body.get("segments") or [] if str(s.get("text", "")).strip()
    ]
    usage = body.get("usage") or {}
    cost = usage.get("cost")
    return RawTranscript(
        text=str(body.get("text") or "").strip(),
        words=words,
        segments=segments,
        language=body.get("language"),
        cost=float(cost) if cost is not None else None,
    )
