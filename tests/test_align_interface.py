import json

import pytest

from yakumi import pipeline
from yakumi.align import AlignError, NoopAligner, check_alignment, create_aligner
from yakumi.asr.types import Word
from yakumi.pipeline import Job, run
from yakumi.stats import format_stats, stats_report


def test_noop_and_factory():
    words = [Word(0, 1, "a")]
    assert NoopAligner().align(None, words) is words
    assert create_aligner({}).name == "none"
    with pytest.raises(AlignError):
        create_aligner({"backend": "nope"})


def test_alignment_must_not_change_text():
    before = [Word(0, 1, "a"), Word(1, 2, "b")]
    check_alignment(before, [Word(0.1, 0.9, "a"), Word(1.1, 1.9, "b")])
    with pytest.raises(AlignError):
        check_alignment(before, [Word(0, 1, "a"), Word(1, 2, "c")])
    with pytest.raises(AlignError):
        check_alignment(before, before[:1])


class ShiftAligner:
    """把每个词的结束时间提前 0.05 秒，模拟“校准”。"""

    name = "shift"

    def align(self, audio, words, language=None):
        assert audio.is_file()  # 对齐在音频删除之前进行
        return [Word(w.start, w.end - 0.05, w.text) for w in words]


def test_pipeline_runs_aligner(fake_cfg, sample_video, monkeypatch):
    monkeypatch.setattr(pipeline, "create_aligner", lambda cfg: ShiftAligner())
    job = Job.create(fake_cfg["workspace"]["dir"], sample_video, "zh")
    state = run(job, fake_cfg)
    words = json.loads(job.path("words.json").read_text())["words"]
    assert words[0]["end"] == pytest.approx(0.35)
    assert state.stats["aligner"] == "shift"


def test_format_stats():
    assert format_stats({}) == ""
    text = format_stats({"mode": "words", "windows": 3, "asr_requests": 4, "asr_cost": 0.09,
                         "repairs_found": 1, "repairs_tried": 1, "repair_requests": 3, "repairs_accepted": 1,
                         "repair_cost": 0.001})
    assert text == "识别：词级时间戳，3 个窗口，4 次请求，复核 1 处，采用 1 处；费用 $0.0910"
    text = format_stats({"mode": "words", "windows": 3, "asr_requests": 3, "asr_cost": 0.29,
                         "translate_requests": 45, "translate_cost": 0.2, "translate_effort": "medium",
                         "translate_reasoning_tokens": 52300})
    assert text == ("识别：词级时间戳，3 个窗口，3 次请求；翻译：45 次请求，思考档位 medium，思考 52.3k token；"
                    "费用 $0.4900")


def test_stats_report_sections():
    assert stats_report({}) is None
    report = stats_report({"mode": "text", "windows": 2, "asr_requests": 3, "asr_failed": 1, "asr_cost": 0.1,
                           "translate_requests": 4, "translate_unit": "sentence", "translate_cost": 0.05,
                           "translate_split_retries": 2, "translate_cost_unknown": 1})
    assert [s["label"] for s in report["sections"]] == ["识别", "翻译"]
    asr = report["sections"][0]["items"]
    assert [i["text"] for i in asr] == ["纯文本", "2 个窗口", "3 次请求", "失败 1 次"]
    assert [i["warn"] for i in asr] == [False, False, False, True]
    assert report["cost"] == "$0.1500"
    assert report["cost_notes"] == ["另有 1 次未返回费用", "失败请求的费用未计入"]
    assert format_stats({"mode": "text", "windows": 2, "asr_requests": 3, "asr_failed": 1, "asr_cost": 0.1}) == (
        "识别：纯文本，2 个窗口，3 次请求，失败 1 次；费用 $0.1000（失败请求的费用未计入）")


def test_stats_with_only_term_scanning_are_reported():
    report = stats_report({"term_entities": 3, "terms_requests": 2, "terms_cost": 0.02, "term_failed_chunks": 1})
    assert report["cost"] == "$0.0200"
    [section] = report["sections"]
    assert section["label"] == "术语扫描" and section["items"][-1] == {"text": "1 块扫描失败", "warn": True}
