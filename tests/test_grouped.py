import json
import re

import pytest

from yakumi.models import Segment
from yakumi.pipeline import Job, run
from yakumi.subtitle.formats import render
from yakumi.subtitle.style import style_line
from yakumi.translate.base import Translator
from yakumi.translate.grouped import (SentenceTranslator, group_by_punctuation, group_cues, split_parts,
                                      split_translation, translate_cues)


class Scripted(Translator):
    def __init__(self, fn, **kw):
        super().__init__(**kw)
        self.fn = fn
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.fn(system, user, len(self.calls))


def numbered(user):
    return re.findall(r"^(\d+)\|(.*)$", user.split("请翻译：", 1)[1], re.M)


def per_part(system, user, n):
    return "\n".join(f"{i}|" + "¦".join(f"T{p}" for p in text.split("¦")) for i, text in numbered(user))


def cues_of(*sentences):
    out, t = [], 0.0
    for gid, parts in enumerate(sentences):
        for text in parts:
            out.append(Segment(t, t + 1, text, group=gid))
            t += 1
    return out


def test_group_cues_and_batches():
    cues = cues_of(["a", "b"], ["c"], ["d", "e", "f"])
    cues.append(Segment(9, 10, "x"))  # 未分组
    cues.append(Segment(10, 11, "y"))
    assert group_cues(cues) == [[0, 1], [2], [3, 4, 5], [6], [7]]
    st = SentenceTranslator(Scripted(per_part), batch_sentences=2)
    assert st._batches(group_cues(cues)) == [[[0, 1], [2]], [[3, 4, 5], [6]], [[7]]]


def test_sentence_translation_direct():
    cues = cues_of(["音が", "ちょっと", "微妙かな。"], ["次です。"])
    tr = Scripted(per_part)
    texts, counters = translate_cues(tr, cues, "zh", "ja")
    assert texts == ["T音が", "Tちょっと", "T微妙かな。", "T次です。"]
    system, user = tr.calls[0]
    assert "1|音が¦ちょっと¦微妙かな。" in user and "2|次です。" in user
    assert "¦" in system
    assert counters["sentences"] == 2 and counters["split_retries"] == 0


def test_mismatched_parts_retried_individually():
    cues = cues_of(["a", "b"], ["c", "d"])

    def reply(system, user, n):
        if n == 1:
            return "1|AB\n2|C¦D"  # 第 1 句段数不对
        assert numbered(user) == [("1", "a¦b")]  # 只重试出错的句子
        return "1|A¦B"

    texts, counters = translate_cues(Scripted(reply), cues, "zh")
    assert texts == ["A", "B", "C", "D"]
    assert counters["split_retries"] == 1 and counters["split_proportional"] == 0


def test_proportional_split_fallback():
    cues = cues_of(["音が", "ちょっと微妙かな。"])

    def reply(system, user, n):
        if "¦" in system:  # 分段请求一直不对
            return "1|声音好像有点微妙吧。"
        return "1|声音，好像有点微妙吧。"

    texts, counters = translate_cues(Scripted(reply), cues, "zh")
    assert texts == ["声音，", "好像有点微妙吧。"]
    assert counters["split_proportional"] == 1


def test_per_cue_fallback_when_translation_too_short():
    cues = cues_of(["a", "b", "c"])

    def reply(system, user, n):
        if "¦" in system:
            return "1|x"
        lines = numbered(user)
        if len(lines) == 1 and lines[0][1] == "a b c":
            return "1|嗯"  # 整句译文只有 1 个字，切不成 3 段
        return "\n".join(f"{i}|{t.upper()}" for i, t in lines)

    texts, counters = translate_cues(Scripted(reply), cues, "zh")
    assert texts == ["A", "B", "C"]
    assert counters["split_per_cue"] == 1


def test_concurrent_sentence_translation_keeps_order():
    cues = cues_of(*[[f"s{i}a", f"s{i}b"] for i in range(20)])
    tr = Scripted(per_part, concurrency=4)
    texts, _ = translate_cues(tr, cues, "zh", batch_sentences=3)
    assert texts == [f"T{c.text}" for c in cues]
    assert len(tr.calls) == 7


def test_cue_unit_uses_plain_translation():
    cues = cues_of(["a", "b"])
    tr = Scripted(lambda s, u, n: "\n".join(f"{i}|T{t}" for i, t in numbered(u)))
    texts, counters = translate_cues(tr, cues, "zh", unit="cue")
    assert texts == ["Ta", "Tb"] and counters == {}
    assert "¦" not in tr.calls[0][1]


def test_split_parts():
    assert split_parts("a¦b", 2) == ["a", "b"]
    assert split_parts("a｜b", 2) == ["a", "b"]
    assert split_parts("a¦", 2) is None
    assert split_parts("你¦好", 1) == ["你好"]
    assert split_parts("", 1) is None


def test_split_translation():
    assert split_translation("声音，好像有点微妙吧。", [2, 8]) == ["声音，", "好像有点微妙吧。"]
    assert split_translation("The sound is a bit off, I think.", [5, 5]) == ["The sound is a bit off,", "I think."]
    assert split_translation("嗯", [1, 1]) is None
    parts = split_translation("一二三四五六七八九十", [1, 1, 1, 1, 1])
    assert parts and len(parts) == 5 and "".join(parts) == "一二三四五六七八九十"


def test_group_by_punctuation():
    cues = [Segment(0, 1, "音が"), Segment(1, 2, "微妙かな。"), Segment(2, 3, "次"), Segment(6, 7, "後")]
    group_by_punctuation(cues)
    assert [c.group for c in cues] == [0, 0, 1, 2]


# ---- 字幕标点惯例 ----------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("声音好像有点微妙吧。", "声音好像有点微妙吧"),
    ("好的，那我们开始吧。", "好的 那我们开始吧"),
    ("真的吗？太好了！", "真的吗？太好了！"),
    ("嗯...我想想", "嗯……我想想"),
    ("苹果、香蕉、橘子，", "苹果、香蕉、橘子"),
    ("他说：“走吧。”", "他说：“走吧”"),
    ("版本 3.5 发布了，", "版本 3.5 发布了"),
    ("真的?", "真的？"),
    ("Hello, world.", "Hello, world."),
    ("Konkoyo。", "Konkoyo"),
    ("OK！", "OK！"),
    ("音が、ちょっと微妙かな。", "音が　ちょっと微妙かな"),
])
def test_subtitle_punctuation(text, expected):
    assert style_line(text) == expected


def test_keep_punctuation_and_bilingual_render():
    segs = [Segment(0, 1, "微妙かな。", "有点微妙吧。")]
    assert "有点微妙吧。" in render(segs, "srt", punctuation="keep")
    out = render(segs, "srt", bilingual=True, punctuation="subtitle")
    assert "有点微妙吧\n微妙かな\n" in out


# ---- 流水线：整句翻译与迁移 --------------------------------------------------
def test_pipeline_records_sentences_and_groups(fake_cfg, sample_video):
    job = Job.create(fake_cfg["workspace"]["dir"], sample_video, "zh")
    state = run(job, fake_cfg)
    sentences = json.loads(job.path("sentences.json").read_text())
    cues = job.segments()
    assert sentences and all(c.group >= 0 for c in cues)
    assert {c.group for c in cues} == {s["id"] for s in sentences}
    assert state.stats["segment_mode"] == "llm" and state.stats["translate_unit"] == "sentence"
    # 编辑后保存，分组不丢
    cues[0].translation = "改"
    job.save_segments(cues)
    assert job.segments()[0].group == cues[0].group


def test_v3_job_keeps_transcription(fake_cfg, sample_video):
    ws = fake_cfg["workspace"]["dir"]
    job = Job.create(ws, sample_video, "zh")
    run(job, fake_cfg)
    pieces = sorted(p.name for p in job.path("pieces").iterdir())
    # 伪造一个版本 3 的任务
    data = json.loads(job.path("state.json").read_text())
    data["pipeline_version"] = 3
    data["completed"] = ["extract", "vad", "transcribe", "merge", "repair", "align", "translate", "write"]
    job.path("state.json").write_text(json.dumps(data))
    loaded = Job.load(ws, job.state.id)
    assert loaded.state.completed == ["extract", "vad", "transcribe", "merge", "repair"]
    assert loaded.state.pipeline_version == 4
    assert sorted(p.name for p in loaded.path("pieces").iterdir()) == pieces
    assert not loaded.path("translated.json").exists()
    calls_before = loaded.state.stats["asr_requests"]
    run(loaded, fake_cfg)
    assert loaded.state.stats["asr_requests"] == calls_before  # 没有重新识别
    assert loaded.path("sentences.json").is_file()


def test_old_job_without_migration_restarts(fake_cfg, sample_video):
    ws = fake_cfg["workspace"]["dir"]
    job = Job.create(ws, sample_video, "zh")
    run(job, fake_cfg)
    data = json.loads(job.path("state.json").read_text())
    data["pipeline_version"] = 2
    job.path("state.json").write_text(json.dumps(data))
    loaded = Job.load(ws, job.state.id)
    assert loaded.state.completed == [] and not loaded.path("pieces").exists()
