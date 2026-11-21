import httpx

from yakumi.translate.base import Translator, parse_numbered
from yakumi.translate.openai_compat import OpenAICompatTranslator


class Scripted(Translator):
    """按脚本依次返回回复，并记录请求。"""

    def __init__(self, replies, **kw):
        super().__init__(**kw)
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append(user)
        reply = self.replies.pop(0)
        return reply(user) if callable(reply) else reply


def echo(user):
    import re

    body = user.split("请翻译：", 1)[1]
    return "\n".join(f"{n}|T:{t}" for n, t in re.findall(r"^(\d+)\|(.*)$", body, re.M))


def test_parse_numbered_ignores_noise_and_think():
    text = "<think>1|nope</think>\nSure:\n1|甲\n2｜乙\n"
    assert parse_numbered(text) == {1: "甲", 2: "乙"}


def test_batches_and_context():
    t = Scripted([echo, echo], batch_size=2, context_lines=1)
    out = t.translate(["a", "b", "c"], "zh")
    assert out == ["T:a", "T:b", "T:c"]
    assert "b => T:b" in t.prompts[1]


def test_missing_line_retries_then_falls_back_to_single():
    t = Scripted(["1|x", "1|x", "1|A", "1|B"], batch_size=5)
    out = t.translate(["a", "b"], "zh")
    assert out == ["A", "B"]
    assert len(t.prompts) == 4


def test_retry_succeeds_second_time():
    t = Scripted(["1|x\n3|y", echo], batch_size=5)
    assert t.translate(["a", "b"], "zh") == ["T:a", "T:b"]


def test_progress_callback():
    seen = []
    Scripted([echo, echo], batch_size=1).translate(["a", "b"], "zh", progress=lambda d, n: seen.append((d, n)))
    assert seen == [(1, 2), (2, 2)]


def test_openai_compat_request_and_retry(monkeypatch):
    import yakumi.llm as mod

    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"choices": [{"message": {"content": "1|你好"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    t = OpenAICompatTranslator("http://x/v1/", "m", "key", client=client)
    assert t.translate(["hello"], "zh") == ["你好"]
    assert str(calls[-1].url) == "http://x/v1/chat/completions"
    assert calls[-1].headers["authorization"] == "Bearer key"


# ---- OpenRouter：思考档位、路由模式、用量 ----------------------------------------
import json as _json  # noqa: E402

import pytest  # noqa: E402

from yakumi.translate import create_translator  # noqa: E402
from yakumi.translate.base import TranslateError  # noqa: E402

OR = "https://openrouter.ai/api/v1"


def reply(content, cost=0.001, reasoning=100):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 300, "completion_tokens": 400, "cost": cost,
                  "completion_tokens_details": {"reasoning_tokens": reasoning}},
    })


def test_openrouter_payload_effort_routing_provider():
    t = OpenAICompatTranslator(OR, "google/gemini-3.8-flash", reasoning_effort="Medium", routing="floor",
                               provider={"sort": "price", "allow_fallbacks": True})
    body = t.payload("sys", "user")
    assert body["model"] == "google/gemini-3.8-flash:floor"
    assert body["reasoning"] == {"effort": "medium", "exclude": True}
    assert body["provider"] == {"sort": "price", "allow_fallbacks": True}
    assert "max_tokens" not in body  # 思考 token 计入 max_tokens，不能设


def test_routing_default_and_explicit_variant():
    assert OpenAICompatTranslator(OR, "m").payload("s", "u")["model"] == "m"
    # 模型名里已经写了变体时不再追加
    assert OpenAICompatTranslator(OR, "m:nitro", routing="floor").payload("s", "u")["model"] == "m:nitro"
    with pytest.raises(TranslateError):
        OpenAICompatTranslator(OR, "m", routing="fastest")


def test_non_openrouter_uses_standard_param_only():
    t = OpenAICompatTranslator("http://127.0.0.1:11434/v1", "qwen", reasoning_effort="low", routing="floor",
                               provider={"sort": "price"})
    body = t.payload("s", "u")
    assert body["model"] == "qwen" and body["reasoning_effort"] == "low"
    assert "reasoning" not in body and "provider" not in body


def test_effort_none_and_unset():
    assert OpenAICompatTranslator(OR, "m", reasoning_effort="none").payload("s", "u")["reasoning"] == {"enabled": False}
    assert "reasoning" not in OpenAICompatTranslator(OR, "m").payload("s", "u")


def test_mandatory_reasoning_error_is_explained():
    body = {"error": {"message": "Reasoning is mandatory for this endpoint and cannot be disabled.", "code": 400}}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json=body)))
    t = OpenAICompatTranslator(OR, "google/gemini-3.8-flash", reasoning_effort="none", client=client)
    with pytest.raises(TranslateError, match="reasoning_effort"):
        t.translate(["hi"], "zh")
    assert t.usage["failed"] == 1


def test_usage_accumulates():
    def handler(request):
        user = _json.loads(request.content)["messages"][1]["content"]
        return reply(echo(user), cost=0.002, reasoning=50)

    t = OpenAICompatTranslator(OR, "m", client=httpx.Client(transport=httpx.MockTransport(handler)), batch_size=2)
    assert t.translate(["a", "b", "c"], "zh") == ["T:a", "T:b", "T:c"]
    assert t.usage["requests"] == 2 and t.usage["reasoning_tokens"] == 100
    assert t.usage["cost"] == pytest.approx(0.004)


def test_concurrent_translation_keeps_order_and_uses_source_context():
    import threading
    import time as _time

    prompts = []
    lock = threading.Lock()

    class Slow(Translator):
        def complete(self, system, user):
            with lock:
                prompts.append(user)
            # 让前面的批更慢，验证结果仍按原顺序拼回
            first = int(user.split("请翻译：", 1)[1].split("|", 1)[1].split("\n", 1)[0][1:])
            _time.sleep(0.05 * (10 - first) / 10)
            return echo(user)

    lines = [f"L{i}" for i in range(10)]
    out = Slow(batch_size=2, concurrency=4, context_lines=2).translate(lines, "zh")
    assert out == [f"T:L{i}" for i in range(10)]
    ctx = next(p for p in prompts if "1|L4" in p)
    assert "上文（仅供参考，不要翻译）：\nL2\nL3\n" in ctx and "=>" not in ctx


def test_factory_reads_new_options():
    t = create_translator({"provider": "openai", "base_url": OR, "model": "google/gemini-3.8-flash",
                           "reasoning_effort": "low", "routing": "nitro", "provider_routing": {"order": ["google-ai-studio"]},
                           "concurrency": 3})
    body = t.payload("s", "u")
    assert body["model"] == "google/gemini-3.8-flash:nitro" and body["provider"] == {"order": ["google-ai-studio"]}
    assert t.concurrency == 3
