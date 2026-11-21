import pytest

from yakumi.asr.types import Word
from yakumi.llm import LLMError
from yakumi.segment import create_segmenter, segment_words
from yakumi.segment.common import cap_spans, check_spans, join_texts, tokenize
from yakumi.segment.cues import build_cues
from yakumi.segment.llm import FakeChat, LLMSegmenter, map_breaks, plan_chunks
from yakumi.segment.rules import rule_spans

SUB = {"max_chars": 42, "max_chars_cjk": 20, "max_duration": 6.0, "min_duration": 0.8}


def words_from(spec):
    """spec: [(text, start, end)]"""
    return [Word(s, e, t) for t, s, e in spec]


def timeline(texts, step=0.4, pauses=None):
    """依次排列的词；pauses = {词下标: 该词之后的额外停顿}。"""
    pauses = pauses or {}
    out, t = [], 0.0
    for i, text in enumerate(texts):
        out.append(Word(round(t, 3), round(t + step, 3), text))
        t += step + pauses.get(i, 0.0)
    return out


class Scripted:
    def __init__(self, fn):
        self.fn = fn
        self.calls = []
        self.usage = {"requests": 0}

    def complete(self, system, user):
        self.calls.append(user)
        return self.fn(user)


# ---- 换行映射 -------------------------------------------------------------
JA = ["今夜", "は", "ASMR", "です", "お帰り", "ありがと", "叩く", "よ。"]


def test_map_breaks_exact():
    assert map_breaks(JA, "今夜はASMRです\nお帰りありがと\n叩くよ。") == [4, 6]


def test_map_breaks_ignores_whitespace_and_fences():
    assert map_breaks(JA, "```\n今夜は ASMR です\n\nお帰りありがと叩くよ。\n```") == [4]


def test_map_breaks_minor_change_mapped_through_diff():
    texts = ["えっと", "それは", "ね…", "そう", "なんだ", "けど", "でもね", "今日は", "ちょっと", "眠い", "かも。"]
    # 省略号写法不同（… → ...）、一个半角感叹号，其余不变
    reply = "えっとそれはね...\nそうなんだけど\nでもね今日はちょっと眠いかも!"
    assert map_breaks(texts, reply) is None  # 差异超过 2%
    long = texts * 6
    src = "".join(long)
    lines = [src[:60].replace("…", "...", 1), src[60:]]
    cuts = map_breaks(long, "\n".join(lines))
    assert cuts and len(cuts) == 1
    # 换行落在第 60 个字附近的词边界
    pos = sum(len(t) for t in long[:cuts[0]])
    assert abs(pos - 60) <= 3


def test_map_breaks_rejects_rewritten_text():
    assert map_breaks(JA, "今晚是ASMR\n欢迎回来") is None
    assert map_breaks(JA, "") is None


def test_map_breaks_snaps_to_nearest_word_boundary():
    # 换行落在 "ありがと" 中间（"あり|がと"），离左边界更近 → 切在它之前
    assert map_breaks(JA, "今夜はASMRですお帰りあ\nりがと叩くよ。") == [5]
    assert map_breaks(JA, "今夜はASMRですお帰りありが\nと叩くよ。") == [6]


def test_map_breaks_latin():
    texts = ["Hello", "there.", "How", "are", "you", "doing", "today?"]
    assert map_breaks(texts, "Hello there.\nHow are you doing today?") == [2]


# ---- 分块 ---------------------------------------------------------------
def test_plan_chunks_prefers_strong_boundaries():
    words = timeline(["ああああ"] * 60, pauses={9: 0.3, 24: 2.5, 40: 0.5})  # 240 字
    chunks = plan_chunks(words, 100)
    check_spans(chunks, len(words))
    # 目标 100 字（25 个词）：第 25 个词后有 2.5 秒停顿，是强边界
    assert chunks[0] == (0, 25)


def test_plan_chunks_short_input_single_chunk():
    words = timeline(["a"] * 10)
    assert plan_chunks(words, 400) == [(0, 10)]


# ---- LLM 断句 ------------------------------------------------------------
def _ja_words(n=40):
    base = ["音が", "ちょっと", "微妙", "かな。", "さっき", "確認", "してた", "ときは", "もうちょっと", "いけてた", "んだけど。"]
    return timeline([base[i % len(base)] for i in range(n)], pauses={0: 1.5, 1: 1.5, 4: 2.0})


def test_llm_segmenter_keeps_text_and_covers_words():
    words = _ja_words(80)
    seg = LLMSegmenter(FakeChat(), chunk_chars=60, concurrency=3)
    spans = seg.split(words)
    check_spans(spans, len(words))
    assert join_texts([join_texts([w.text for w in words[i:j]]) for i, j in spans]) == \
        join_texts([w.text for w in words])
    assert seg.stats["chunks"] > 1 and seg.stats["fallbacks"] == 0


def test_llm_segmenter_falls_back_per_chunk():
    words = _ja_words(80)
    calls = {"n": 0}

    def reply(text):
        calls["n"] += 1
        return "全部改写了" if calls["n"] == 1 else FakeChat().complete("", text)

    seg = LLMSegmenter(Scripted(reply), chunk_chars=60, concurrency=1)
    spans = seg.split(words)
    check_spans(spans, len(words))
    assert seg.stats["fallbacks"] == 1


def test_llm_segmenter_raises_when_every_request_fails():
    def boom(text):
        raise LLMError("401 invalid key")

    with pytest.raises(LLMError, match="401"):
        LLMSegmenter(Scripted(boom), chunk_chars=60).split(_ja_words(80))


def test_llm_segmenter_partial_request_failure_uses_rules():
    words = _ja_words(80)
    n = {"i": 0}

    def flaky(text):
        n["i"] += 1
        if n["i"] == 2:
            raise LLMError("timeout")
        return FakeChat().complete("", text)

    seg = LLMSegmenter(Scripted(flaky), chunk_chars=60, concurrency=1)
    check_spans(seg.split(words), len(words))
    assert seg.stats["fallbacks"] == 1


def test_cross_chunk_sentence_is_joined():
    # 两块之间没有句末标点、也几乎没有停顿：前一块的最后一句和后一块的第一句并成一句
    words = timeline(["あいうえお"] * 30)
    seg = LLMSegmenter(Scripted(lambda text: text), chunk_chars=50, concurrency=1)
    spans = seg.split(words)
    assert seg.stats["chunks"] > 1
    assert spans == cap_spans(words, [(0, 30)], 120)


def test_long_sentence_is_capped():
    words = timeline(["ああああ"] * 50)
    seg = LLMSegmenter(Scripted(lambda text: text), chunk_chars=400, max_sentence_chars=30)
    spans = seg.split(words)
    assert all(sum(len(w.text) for w in words[i:j]) <= 60 for i, j in spans)


def test_create_segmenter_inherits_translate_and_falls_back_without_key():
    cfg = {"translate": {"provider": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "m",
                         "api_key": "k", "reasoning_effort": "medium"},
           "segment": {"mode": "llm", "model": "", "reasoning_effort": "low", "temperature": 0}}
    seg = create_segmenter(cfg)
    assert seg.name == "llm"
    body = seg.chat.payload("s", "u")
    assert body["model"] == "m" and body["reasoning"]["effort"] == "low" and body["temperature"] == 0
    cfg["translate"]["api_key"] = ""
    assert create_segmenter(cfg).name == "rules"
    cfg["segment"]["mode"] = "rules"
    assert create_segmenter(cfg).name == "rules"


# ---- 规则断句 ------------------------------------------------------------
def test_rule_spans_punctuation_and_long_pause_only():
    words = words_from([("Hello", 0, 0.4), ("world.", 0.5, 0.9), ("Next", 1.0, 1.2), ("one", 2.5, 2.8),
                        ("goes", 6.0, 6.3), ("on", 6.4, 6.6)])
    # 1.3 秒的停顿不断句，3.2 秒的停顿断句
    assert rule_spans(words, 120) == [(0, 2), (2, 4), (4, 6)]


def test_tokenize_text_mode():
    assert [t.text for t in tokenize("今日は晴れ。 Hello world.")] == ["今", "日", "は", "晴", "れ。", "Hello", "world."]


# ---- 字幕条 -------------------------------------------------------------
def test_cues_split_on_pause_but_stay_in_sentence():
    words = words_from([("音が", 0.0, 0.5), ("ちょっと", 2.0, 2.6), ("微妙かな。", 4.2, 5.0),
                        ("次", 5.1, 5.4), ("の", 5.4, 5.5), ("話", 5.5, 5.9), ("です。", 5.9, 6.4)])
    sentences, cues = segment_words(words, LLMSegmenter(Scripted(
        lambda t: "音がちょっと微妙かな。\n次の話です。")), SUB, cue_gap=1.2)
    assert [s.text for s in sentences] == ["音がちょっと微妙かな。", "次の話です。"]
    # 停顿处拆开；两个字的碎片并进同一句的下一条
    assert [(c.text, c.group) for c in cues] == [("音がちょっと", 0), ("微妙かな。", 0), ("次の話です。", 1)]
    assert (cues[0].start, cues[0].end) == (0.0, 2.6)
    assert cues[1].end <= cues[2].start


def test_cues_tiny_fragment_merged_within_sentence_only():
    words = words_from([("と", 0.0, 0.2), ("いう", 1.5, 1.9), ("立体音響の配信のことです。", 1.9, 4.0),
                        ("え", 4.1, 4.3)])
    cues = build_cues(words, [(0, 3), (3, 4)], cue_gap=1.2)
    # “と” 后停顿 1.3 秒被拆开，但太短，并进同一句；“え” 是另一句，不跨句合并
    assert [(c.text, c.group) for c in cues] == [("という立体音響の配信のことです。", 0), ("え", 1)]
    assert cues[1].end == pytest.approx(4.9)  # 过短的字幕条延长到 min_duration


def test_cues_long_sentence_split_at_clause():
    texts = ["今日は", "とても", "いい", "天気", "なので、", "みんなで", "公園に", "行って", "お弁当を", "食べ", "ました。"]
    words = timeline(texts, step=0.5)
    cues = build_cues(words, [(0, len(words))], max_chars_cjk=10, max_duration=10)
    assert cues[0].text.endswith("なので、")
    assert all(c.group == 0 for c in cues)
    assert join_texts([c.text for c in cues]) == join_texts(texts)


def test_cue_text_concatenation_equals_words():
    words = _ja_words(120)
    sentences, cues = segment_words(words, LLMSegmenter(FakeChat(), chunk_chars=80), SUB, cue_gap=1.2)
    full = join_texts([w.text for w in words])
    assert join_texts([s.text for s in sentences]) == full
    assert join_texts([c.text for c in cues]) == full
    groups = [c.group for c in cues]
    assert groups == sorted(groups) and set(groups) == set(range(len(sentences)))
    assert all(a.end <= b.start for a, b in zip(cues, cues[1:]))
