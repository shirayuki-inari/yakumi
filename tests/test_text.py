from yakumi.models import Segment
from yakumi.segment.cues import split_by_ratio
from yakumi.text import char_limit, is_cjk, join_text, join_texts


def test_is_cjk_and_limits():
    assert is_cjk("今天我们来聊一聊") and is_cjk("ASMRの配信です")
    assert not is_cjk("Hello world") and not is_cjk("  ")
    assert char_limit("中文", 42, 20) == 40 and char_limit("abc", 42, 20) == 84


def test_join_by_script():
    assert join_text("Hello", "world.") == "Hello world."
    assert join_text("おい、", "と") == "おい、と"
    assert join_text("ASMR", "の") == "ASMRの"
    assert join_texts(["", "a", "", "b"]) == "a b"


def test_split_by_ratio_prefers_sentence_end_and_keeps_time():
    text = "This is the first sentence of the talk. And here comes another sentence that is long."
    out = split_by_ratio(Segment(0, 10, text), max_duration=6, max_chars=42, max_chars_cjk=20)
    assert [s.text for s in out][:1] == ["This is the first sentence of the talk."]
    assert out[0].start == 0 and out[-1].end == 10
    assert all(abs(a.end - b.start) < 1e-9 for a, b in zip(out, out[1:]))


def test_split_by_ratio_cjk_within_limits():
    text = "今天我们来聊一聊视频本地化，这件事情其实比大家想象的要复杂很多。首先是语音识别的准确率问题。"
    out = split_by_ratio(Segment(0, 8, text), max_duration=10, max_chars=42, max_chars_cjk=20)
    assert len(out) >= 2 and all(len(s.text) <= 40 for s in out)
    assert "".join(s.text for s in out) == text
