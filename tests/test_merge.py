"""重叠区合并：去重、保留真实重复、容忍识别差异、补回一侧漏掉的词。

场景取自实测：同一段音频两次识别的结果会有差异（同音字、分词粒度），
硬切时日语切点上的「見た」丢失。
"""

from yakumi.asr.types import AudioWindow, Word
from yakumi.chunking.merge import dedupe_text, merge_text, merge_words, timed_lcs, to_units


def test_midpoint_fallback_does_not_assume_midpoints_follow_start_order():
    a = AudioWindow(0, 0, 8, 0, 5)
    b = AudioWindow(1, 2, 10, 5, 10)
    words, _ = merge_words([(a, [Word(0, 12, "long"), Word(1, 2, "short")]), (b, [Word(6, 7, "next")])])
    assert [w.text for w in words] == ["short", "next"]

# 切点在 120 秒，强制切分，两侧各重叠 1.5 秒
A = AudioWindow(0, 0, 121.5, 0, 120, None, "forced")
B = AudioWindow(1, 118.5, 240, 120, 240, "forced", None)


def seq(*items):
    """("text", start, end) → Word 列表"""
    return [Word(s, e, t) for t, s, e in items]


def texts(words):
    return [w.text for w in words]


def test_overlap_duplicates_removed():
    a = seq(("well", 117.0, 117.4), ("I", 118.6, 118.7), ("think", 118.8, 119.1), ("so", 119.3, 119.6),
            ("because", 120.2, 120.7), ("the", 120.8, 120.9), ("rain", 121.0, 121.4))
    b = seq(("I", 118.6, 118.7), ("think", 118.8, 119.1), ("so", 119.3, 119.6), ("because", 120.2, 120.7),
            ("the", 120.8, 120.9), ("rain", 121.0, 121.4), ("stopped.", 121.5, 122.0))
    words, [report] = merge_words([(A, a), (B, b)])
    assert texts(words) == ["well", "I", "think", "so", "because", "the", "rain", "stopped."]
    assert report.strategy == "lcs" and not report.risky


def test_text_dedupe_requires_both_edges_and_a_complete_word():
    for a, b in [
        ("We say hello", "Fresh intro hello again"),
        ("We say hello first", "hello again"),
        ("We say hello", "helloworld again"),
    ]:
        assert dedupe_text(a, b) == (b, False)


def test_text_merge_uses_only_adjacent_overlapping_windows():
    a = AudioWindow(0, 0, 3.5, 0, 3, right="forced")
    b = AudioWindow(1, 2.5, 6.5, 3, 6, left="forced", right="forced")
    c = AudioWindow(2, 5.5, 9, 6, 9, left="forced")
    blocks, reports = merge_text([(a, "We say hello"), (b, ""), (c, "hello again")])
    assert [block.text for block in blocks] == ["We say hello", "hello again"]
    assert all(r.risky for r in reports)

    for start in (3.5, 3.45):
        nearly_disjoint = AudioWindow(1, start, 6, start, 6, left="silence")
        blocks, _ = merge_text([(a, "We say hello"), (nearly_disjoint, "hello again")])
        assert len(blocks) == 2 and blocks[1].text == "hello again"


def test_real_repetition_is_kept():
    # “no no no” 横跨切点；时间约束让每个 no 只和同一时刻的 no 匹配
    a = seq(("no", 119.0, 119.3), ("no", 119.5, 119.8), ("no", 120.0, 120.3))
    b = seq(("no", 119.0, 119.3), ("no", 119.5, 119.8), ("no", 120.0, 120.3), ("way.", 120.5, 120.9))
    words, _ = merge_words([(A, a), (B, b)])
    assert texts(words) == ["no", "no", "no", "way."]


def test_same_sentence_at_different_times_is_kept():
    # 同一句话在窗口 A 的前部和重叠区各说了一次，不能当作重复去掉
    a = seq(("thank", 10.0, 10.3), ("you", 10.4, 10.6), ("thank", 119.2, 119.5), ("you", 119.6, 119.8))
    b = seq(("thank", 119.2, 119.5), ("you", 119.6, 119.8), ("bye", 121.0, 121.3))
    words, _ = merge_words([(A, a), (B, b)])
    assert texts(words) == ["thank", "you", "thank", "you", "bye"]


def test_tolerates_recognition_differences_and_jitter():
    # 两次识别：同音字不同（法昌 / 宝生），分词粒度不同（が ある。 / がある。），时间差 0.1 秒
    a = seq(("法昌", 117.8, 118.3), ("の", 118.3, 118.4), ("舞台", 118.4, 118.7), ("で", 118.7, 118.8),
            ("高砂", 119.3, 119.8), ("を", 119.8, 119.9), ("見", 119.9, 120.0), ("た", 120.0, 120.1),
            ("こと", 120.1, 120.3), ("が", 120.3, 120.4), ("ある。", 120.4, 120.6))
    b = seq(("宝生", 118.0, 118.3), ("の", 118.3, 118.4), ("舞台", 118.4, 118.7), ("で", 118.7, 118.8),
            ("高砂", 119.2, 119.7), ("を", 119.7, 119.8), ("見た", 119.9, 120.1), ("こと", 120.1, 120.3),
            ("がある。", 120.3, 120.6), ("その", 121.4, 121.6))
    words, [report] = merge_words([(A, a), (B, b)])
    # 不重复、不丢字；拼接点前用 A 的版本，之后用 B 的版本
    assert "".join(texts(words)) == "法昌の舞台で高砂を見たことがある。その"
    assert report.strategy == "lcs" and not report.risky


def test_word_missing_on_one_side_is_recovered():
    # 实测：硬切时 A 在切点前丢了「見た」。重叠区里 B 有它，拼接时应取 B 的版本
    a = seq(("高砂", 119.3, 119.8), ("を", 119.8, 119.9), ("こと", 120.1, 120.3), ("が", 120.3, 120.4),
            ("ある。", 120.4, 120.6))
    b = seq(("高砂", 119.3, 119.8), ("を", 119.8, 119.9), ("見た", 119.9, 120.1), ("こと", 120.1, 120.3),
            ("がある。", 120.3, 120.6))
    words, _ = merge_words([(A, a), (B, b)])
    assert "".join(texts(words)) == "高砂を見たことがある。"


def test_no_match_falls_back_and_flags_risk():
    a = seq(("alpha", 119.0, 119.5))
    b = seq(("omega", 120.5, 121.0))
    words, [report] = merge_words([(A, a), (B, b)])
    assert texts(words) == ["alpha", "omega"]
    assert report.strategy == "midpoint" and report.risky


def test_silence_cut_inside_whisper_is_spliced():
    # 实测（耳语 ASMR）：VAD 把说话判成了静音，切点落在「もう1回、左耳でいくね」中间。
    # 两侧有重叠，按时间约束 LCS 拼接，切点上的词只保留一份、不丢
    a_win = AudioWindow(0, 0, 101.5, 0, 100, None, "silence")
    b_win = AudioWindow(1, 98.5, 200, 100, 200, "silence", None)
    a = seq(("もう", 99.0, 99.3), ("1", 99.3, 99.5), ("回、", 99.5, 99.9), ("左", 99.9, 100.1),
            ("耳", 100.1, 100.4), ("で", 100.4, 100.6), ("いく", 100.6, 100.9), ("ね。", 100.9, 101.2))
    b = seq(("1", 99.3, 99.5), ("回、", 99.5, 99.9), ("左", 99.9, 100.1), ("耳", 100.1, 100.4),
            ("で", 100.4, 100.6), ("いく", 100.6, 100.9), ("ね。", 100.9, 101.2), ("はい", 102.0, 102.3))
    words, [report] = merge_words([(a_win, a), (b_win, b)])
    assert "".join(texts(words)) == "もう1回、左耳でいくね。はい"
    assert report.kind == "silence" and report.strategy == "lcs" and not report.risky


def test_empty_overlap_is_not_risky():
    a_win = AudioWindow(0, 0, 101.5, 0, 100, None, "silence")
    b_win = AudioWindow(1, 98.5, 200, 100, 200, "silence", None)
    words, [report] = merge_words([(a_win, seq(("end.", 90.0, 90.5))), (b_win, seq(("start", 110.0, 110.4)))])
    assert texts(words) == ["end.", "start"]
    assert report.strategy == "empty" and not report.risky


def test_clip_edge_fragment_is_not_risky():
    # 实测（1849.8 秒）：A 听到「最終的に」；B 的片段从 1848.3 秒开始，开头 0.4 秒只听到半个音，
    # 识别成「くん」。两者对不上，但 B 的那个词在片段边缘，不应算高风险；按中点归属保留 A 的版本
    a_win = AudioWindow(0, 1548.3, 1851.3, 1548.3, 1849.8, None, "forced")
    b_win = AudioWindow(1, 1848.3, 2160.0, 1849.8, 2160.0, "forced", None)
    a = seq(("最終", 1847.7, 1848.1), ("的", 1848.1, 1848.4), ("に", 1848.4, 1848.6))
    b = seq(("くん", 1848.4, 1848.75), ("はい", 1852.0, 1852.3))
    words, [report] = merge_words([(a_win, a), (b_win, b)])
    assert texts(words) == ["最終", "的", "に", "はい"]
    assert report.strategy == "edge" and not report.risky


def test_inner_mismatch_is_still_risky():
    # 实测（2459.2 秒）：B 没听出很轻的「ふわ」，A 的这个词离片段边缘还有 2 秒 → 仍是高风险
    a_win = AudioWindow(0, 2157.7, 2460.7, 2157.7, 2459.2, None, "forced")
    b_win = AudioWindow(1, 2457.7, 2760.7, 2459.2, 2760.7, "forced", None)
    _, [report] = merge_words([(a_win, seq(("ふわ", 2458.7, 2459.0))), (b_win, [])])
    assert report.strategy == "midpoint" and report.risky


def test_timed_lcs_respects_time():
    a = to_units(seq(("x", 1.0, 1.2), ("y", 2.0, 2.2)))
    b = to_units(seq(("x", 5.0, 5.2), ("y", 2.1, 2.3)))
    assert timed_lcs(a, b, 0.5) == [(1, 1)]


def test_text_mode_dedupe():
    b, found = dedupe_text("今日はいい天気ですね。散歩に行きましょう", "散歩に行きましょう。公園まで")
    assert found and b == "公園まで"
    blocks, reports = merge_text([(A, "I think so because the rain"), (B, "because the rain stopped.")])
    assert [x.text for x in blocks] == ["I think so because the rain", "stopped."]
    assert reports[0].strategy == "text_overlap"
    _, reports = merge_text([(A, "abc"), (B, "xyz")])
    assert reports[0].risky
