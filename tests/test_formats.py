from yakumi.models import Segment
from yakumi.subtitle.formats import parse_srt, to_ass, to_srt, to_vtt


SEGS = [
    Segment(0.5, 2.25, "Hello world.", "你好，世界。"),
    Segment(3661.001, 3663.5, "Second line", ""),
]


def test_srt_timestamps_and_numbering():
    out = to_srt(SEGS)
    assert out.startswith("1\n00:00:00,500 --> 00:00:02,250\n你好，世界。\n")
    assert "2\n01:01:01,001 --> 01:01:03,500\nSecond line\n" in out


def test_bilingual_puts_translation_first():
    out = to_srt(SEGS, bilingual=True)
    assert "你好，世界。\nHello world.\n" in out
    # 没有译文时不重复原文
    assert out.count("Second line") == 1


def test_srt_round_trip_keeps_times():
    parsed = parse_srt(to_srt([Segment(1.2, 3.4, "a b"), Segment(5, 6.789, "c")]))
    assert [(s.start, s.end, s.text) for s in parsed] == [(1.2, 3.4, "a b"), (5.0, 6.789, "c")]


def test_parse_vtt_and_crlf():
    vtt = "WEBVTT\r\n\r\n00:00:01.000 --> 00:00:02.500\r\n<i>hi</i> there\r\n"
    [seg] = parse_srt(vtt)
    assert (seg.start, seg.end, seg.text) == (1.0, 2.5, "hi there")


def test_vtt_header():
    assert to_vtt(SEGS).startswith("WEBVTT\n\n00:00:00.500 --> 00:00:02.250")


def test_ass_escapes_and_bilingual_style():
    out = to_ass([Segment(0, 1, "a{b}", "译")], bilingual=True)
    assert "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,译\\N{\\rOriginal}a\\{b\\}" in out


def test_parse_vtt_without_hours():
    from yakumi.subtitle.formats import parse_srt

    segs = parse_srt("WEBVTT\n\n01:02.500 --> 01:04.000\nshort\n\n00:00:05.000 --> 00:00:06.000\nfull\n")
    assert [(s.start, s.end, s.text) for s in segs] == [(62.5, 64.0, "short"), (5.0, 6.0, "full")]
