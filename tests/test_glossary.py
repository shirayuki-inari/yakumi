import json
import re

import pytest
from click.testing import CliRunner

from yakumi import glossary as gs
from yakumi.cli import main
from yakumi.pipeline import Job, PipelineError, run, translation_config
from yakumi.translate.base import Translator
from yakumi.translate.grouped import translate_cues
from yakumi.models import Segment


def test_save_load_roundtrip_with_tricky_text():
    g = gs.Glossary(name="博衣こより", terms={"こより": "可萝莉", 'a"b': "c\\d", "助手くん": ""},
                    notes="第一行\n第二行 '引号' \"双引号\"")
    gs.save(g)
    back = gs.load("博衣こより")
    assert back.terms == g.terms and back.notes == g.notes
    assert [x.id for x in gs.list_all()] == ["博衣こより"]


def test_notes_with_triple_quotes_roundtrip():
    gs.save(gs.Glossary(name="x", notes="a ''' b"))
    assert gs.load("x").notes == "a ''' b"


def test_invalid_names_and_rename():
    with pytest.raises(gs.GlossaryError):
        gs.Glossary.from_dict({"name": " / "})
    assert gs.glossary_id("a/b:c") == "abc"
    with pytest.raises(gs.GlossaryError):
        gs.path_of("../evil")
    gs.save(gs.Glossary(name="old", terms={"a": "b"}))
    gs.save(gs.Glossary(name="new", terms={"a": "b"}), old_id="old")
    assert [x.id for x in gs.list_all()] == ["new"]
    gs.save(gs.Glossary(name="other"))
    with pytest.raises(gs.GlossaryError, match="已经有"):
        gs.save(gs.Glossary(name="other"), old_id="new")


def test_import_terms_formats():
    text = "# 注释\nこより = 可萝莉\n助手くん\t助手君\n\"Yakumi\",Yakumi\nホロライブ，Hololive\n"
    assert gs.import_terms(text) == {"こより": "可萝莉", "助手くん": "助手君", "Yakumi": "Yakumi",
                                     "ホロライブ": "Hololive"}


def test_resolve_merges_in_order():
    gs.save(gs.Glossary(name="a", terms={"x": "1", "y": "2"}, notes="甲"))
    gs.save(gs.Glossary(name="b", terms={"y": "3"}, notes="乙"))
    terms, notes = gs.resolve({"x": "0", "z": "9"}, ["a", "b"], "本期是 ASMR")
    assert terms == {"x": "1", "y": "3", "z": "9"}
    assert notes == "【a】\n甲\n\n【b】\n乙\n\n【本视频】\n本期是 ASMR"
    assert gs.resolve({}, ["a"])[1] == "甲"
    with pytest.raises(gs.GlossaryError):
        gs.resolve({}, ["missing"])


def test_relevant_terms_only():
    assert gs.relevant_terms({"こより": "可萝莉", "みこ": "Miko", "Yakumi": "八云"},
                             ["こよりです", "yakumi!"]) == {"こより": "可萝莉", "Yakumi": "八云"}


class Capture(Translator):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.systems = []

    def complete(self, system, user):
        self.systems.append(system)
        lines = re.findall(r"^(\d+)\|(.*)$", user.split("请翻译：", 1)[1], re.M)
        return "\n".join(f"{n}|T{t}" for n, t in lines)


def test_prompt_contains_relevant_terms_and_notes():
    tr = Capture(glossary={"こより": "可萝莉", "みこ": "Miko", "助手くん": ""}, notes="主播自称こより")
    cues = [Segment(0, 1, "こよりの", group=0), Segment(1, 2, "助手くん", group=0)]
    translate_cues(tr, cues, "zh")
    system = tr.systems[0]
    assert "こより → 可萝莉" in system and "みこ" not in system
    assert "助手くん（保留原文" in system
    assert "背景说明" in system and "主播自称こより" in system


def test_translation_config_errors_on_missing_glossary():
    with pytest.raises(PipelineError, match="找不到背景资料"):
        translation_config({"glossary": {}}, ["nope"], "")
    gs.save(gs.Glossary(name="g", terms={"a": "b"}, notes="n"))
    tcfg = translation_config({"glossary": {"c": "d"}, "notes": "全局"}, ["g"], "本期")
    assert tcfg["glossary"] == {"c": "d", "a": "b"}
    assert tcfg["notes"].startswith("全局") and "本期" in tcfg["notes"]


def test_job_changes_context_retranslates(fake_cfg, sample_video):
    gs.save(gs.Glossary(name="g", terms={"w1": "一"}))
    ws = fake_cfg["workspace"]["dir"]
    job = Job.create(ws, sample_video, "zh", glossaries=["g"], notes="测试")
    run(job, fake_cfg)
    used = json.loads(job.path("glossary.json").read_text())
    assert used["terms"] == {"w1": "一"} and used["notes"] == "测试"
    assert job.set_translation_context(["g"], "测试") is False
    assert job.set_translation_context([], "测试") is True
    assert "translate" not in job.state.completed and "segment" in job.state.completed


def test_cli_context_commands(tmp_path):
    runner = CliRunner()
    assert "还没有背景资料" in runner.invoke(main, ["context", "list"]).output
    assert runner.invoke(main, ["context", "set", "主播", "こより", "可萝莉"]).exit_code == 0
    assert runner.invoke(main, ["context", "notes", "主播", "VTuber 直播"]).exit_code == 0
    csv = tmp_path / "t.csv"
    csv.write_text("助手くん,助手君\nホロライブ,Hololive\n", encoding="utf-8")
    assert "导入 2 条，共 3 条" in runner.invoke(main, ["context", "import", "主播", str(csv)]).output
    out = runner.invoke(main, ["context", "show", "主播"]).output
    assert "こより → 可萝莉" in out and "VTuber 直播" in out
    assert runner.invoke(main, ["context", "unset", "主播", "こより"]).exit_code == 0
    assert "VTuber 直播  · 2 条固定译法" in runner.invoke(main, ["context", "list"]).output
    assert runner.invoke(main, ["context", "delete", "主播", "--yes"]).exit_code == 0
    assert gs.list_all() == []


def test_cli_translate_with_glossary(tmp_path):
    gs.save(gs.Glossary(name="g", terms={"hello": "你好"}))
    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    runner = CliRunner()
    res = runner.invoke(main, ["translate", str(srt), "-t", "zh", "--translator", "fake", "-x", "g"])
    assert res.exit_code == 0, res.output
    res = runner.invoke(main, ["translate", str(srt), "-t", "zh", "--translator", "fake", "-g", "g"])  # 旧选项
    assert res.exit_code == 0, res.output
    res = runner.invoke(main, ["translate", str(srt), "-t", "zh", "--translator", "fake", "-g", "missing"])
    assert res.exit_code != 0 and "找不到背景资料" in res.output


def test_prompt_marks_source_as_uncertain_asr():
    tr = Capture()
    translate_cues(tr, [Segment(0, 1, "a", group=0)], "zh")
    assert "自动语音识别" in tr.systems[0] and "按正确的意思翻译" in tr.systems[0]
    plain = Capture(source_kind="subtitle")
    plain.translate(["a"], "zh")
    assert "已有的字幕" in plain.systems[0] and "自动语音识别" not in plain.systems[0]


def test_legacy_glossary_command_still_works():
    runner = CliRunner()
    assert runner.invoke(main, ["glossary", "notes", "旧", "说明"]).exit_code == 0
    assert "旧  说明" in runner.invoke(main, ["glossary", "list"]).output
    assert "glossary" not in runner.invoke(main, ["--help"]).output
