import httpx
import pytest

from yakumi.asr.base import ASRError, ASRTimeout, ASRTooLarge
from yakumi.asr.openai_compat import OpenAICompatASR
from yakumi.config import load_config

# 与实测的 OpenRouter + Gemini 3.5 Transcribe 返回格式一致
VERBOSE = {
    "task": "transcribe",
    "text": "Hello world. Again",
    "segments": [{"id": 0, "start": 0, "end": 2.0, "text": "Hello world. Again"}],
    "words": [{"word": "Hello", "start": 0.1, "end": 0.4}, {"word": "world.", "start": 0.5, "end": 0.9},
              {"word": "Again", "start": 1.6, "end": 2.0}],
    "usage": {"total_tokens": 843, "input_tokens": 751, "output_tokens": 92, "cost": 0.002606},
}


@pytest.fixture
def audio(tmp_path):
    p = tmp_path / "a.flac"
    p.write_bytes(b"fake")
    return p


def make(handler, **kw):
    return OpenAICompatASR("http://x/v1", "m", "k", client=httpx.Client(transport=httpx.MockTransport(handler)),
                           retries=0, **kw)


def test_verbose_json_words_and_cost(audio):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=VERBOSE)

    asr = make(handler)
    result = asr.transcribe(audio, "ja-JP")
    assert seen["url"] == "http://x/v1/audio/transcriptions" and seen["auth"] == "Bearer k"
    body = seen["body"]
    assert b"verbose_json" in body and body.count(b'name="timestamp_granularities[]"') == 2
    assert b'name="language"\r\n\r\nja\r\n' in body
    assert b"audio/flac" in body
    assert [(w.start, w.end, w.text) for w in result.words] == [(0.1, 0.4, "Hello"), (0.5, 0.9, "world."),
                                                                 (1.6, 2.0, "Again")]
    assert result.cost == pytest.approx(0.002606)
    assert result.language is None and result.elapsed is not None
    assert asr.capabilities().word_timestamps


def test_falls_back_to_text_when_verbose_rejected(audio):
    formats = []

    def handler(request):
        fmt = b"verbose_json" if b"verbose_json" in request.content else b"json"
        formats.append(fmt)
        if fmt == b"verbose_json":
            return httpx.Response(400, json={"error": {"message": "response_format verbose_json not supported"}})
        return httpx.Response(200, json={"text": "only text", "usage": {"cost": 0.001}})

    asr = make(handler)
    result = asr.transcribe(audio)
    assert result.text == "only text" and result.words == []
    assert not asr.capabilities().word_timestamps
    asr.transcribe(audio)  # 降级后不再尝试 verbose_json
    assert formats == [b"verbose_json", b"json", b"json"]


def test_words_mode_refuses_fallback(audio):
    asr = make(lambda r: httpx.Response(400, text="verbose_json unsupported"), mode="words")
    with pytest.raises(ASRError, match="词级时间戳"):
        asr.transcribe(audio)


@pytest.mark.parametrize("status,text", [(504, "gateway"), (408, "x"), (502, "upstream request timed out")])
def test_timeout_statuses(audio, status, text):
    with pytest.raises(ASRTimeout):
        make(lambda r: httpx.Response(status, text=text)).transcribe(audio)


def test_client_timeout(audio):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ASRTimeout):
        make(handler).transcribe(audio)


def test_too_large(audio):
    msg = '{"error":{"message":"Audio file exceeds the 25 MB multipart upload limit.","code":400}}'
    with pytest.raises(ASRTooLarge):
        make(lambda r: httpx.Response(400, text=msg)).transcribe(audio)


def test_retries_transient_errors(audio, monkeypatch):
    import yakumi.asr.openai_compat as mod

    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503 if len(calls) == 1 else 200, json=VERBOSE)

    asr = OpenAICompatASR("http://x/v1", "m", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert asr.transcribe(audio).words
    assert len(calls) == 2


def test_config_precedence(tmp_path, monkeypatch):
    path = tmp_path / "c.toml"
    path.write_text('[translate]\nmodel = "from-file"\n[subtitle]\nbilingual = true\n', encoding="utf-8")
    monkeypatch.setenv("YAKUMI_TRANSLATE_MODEL", "from-env")
    monkeypatch.delenv("YAKUMI_ASR_API_KEY", raising=False)
    monkeypatch.delenv("YAKUMI_TRANSLATE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENAI_API_KEY", "oa-key")
    cfg = load_config(path)
    assert cfg["translate"]["model"] == "from-env"
    assert cfg["subtitle"]["bilingual"] is True
    assert cfg["asr"]["api_key"] == "or-key"  # OpenRouter 优先于 OpenAI
    assert cfg["asr"]["base_url"] == "https://openrouter.ai/api/v1"
    cfg = load_config(path, overrides={"translate": {"model": "from-cli", "provider": None}})
    assert cfg["translate"]["model"] == "from-cli" and cfg["translate"]["provider"] == "openai"
