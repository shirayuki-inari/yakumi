import json
import re

import pytest
from click.testing import CliRunner

from yakumi import glossary as gs
from yakumi.cli import main
from yakumi.pipeline import Job, PipelineError, run, translation_config


def test_stale_glossary_snapshot_cannot_replace_a_newer_update():
    saved = gs.save(gs.Glossary("共有"))
    first, second = gs.load(saved.id), gs.load(saved.id)
    first.notes, second.notes = "第一版", "旧草稿"
    gs.save(first)
    with pytest.raises(gs.GlossaryConflict, match="已更新"):
        gs.save(second)
    assert gs.load(saved.id).notes == "第一版"


def test_glossary_notes_round_trip_and_rename_failure_preserves_old_file(monkeypatch):
    g = gs.save(gs.Glossary("旧名称", notes="一\x01行\x7f"))
    assert gs.load(g.id).notes == g.notes
    old_id, g.name = g.id, "新名称"

    def fail_rename(*_):
        raise OSError("写入失败")

    monkeypatch.setattr(gs, "_write_file", fail_rename)
    with pytest.raises(OSError, match="写入失败"):
        gs.save(g, old_id=old_id)
    assert gs.load(old_id).notes == g.notes
    assert not gs.path_of(g.id).exists()


def test_glossary_batch_restores_written_files_if_commit_fails():
    guide = gs.save(gs.Glossary("g", notes="原始资料"))
    guide.notes = "尝试修改"

    def fail_commit():
        raise OSError("审核无法写入")

    with pytest.raises(OSError, match="审核无法写入"):
        gs.save_many([guide], fail_commit)
    assert gs.load("g").notes == "原始资料"
from yakumi.translate.base import Translator
from yakumi.translate.grouped import translate_cues
from yakumi.models import Segment


def E(canonical, translation=None, **kw):
    return gs.Entry(canonical=canonical, translations={} if translation is None else {"*": translation}, **kw)


def test_save_load_roundtrip_with_tricky_text():
    g = gs.Glossary(name="博衣こより", entries=[
        E("こより", "可萝莉", aliases=["こよ", "博衣"], category="person"), E('a"b', "c\\d"), E("助手くん", ""),
        gs.Entry(canonical="白上フブキ", id="shirakami-fubuki", translations={"zh": "白上吹雪", "en": "Fubuki"})],
        notes="第一行\n第二行 '引号' \"双引号\"")
    gs.save(g)
    back = gs.load("博衣こより")
    assert [e.to_dict() for e in back.entries] == [e.to_dict() for e in g.entries] and back.notes == g.notes
    assert [x.id for x in gs.list_all()] == ["博衣こより"]
    text = gs.path_of("博衣こより").read_text(encoding="utf-8")
    assert text.startswith("version = 2") and "[[entries]]" in text and 'id = "shirakami-fubuki"' in text


def test_legacy_terms_file_reads_as_entities(tmp_path):
    root = tmp_path / "g"
    root.mkdir()
    (root / "旧.toml").write_text('name = "旧"\nnotes = "说明"\n\n[terms]\n"フブキ" = "吹雪"\n"フブちゃん" = "吹雪"\n'
                                 '"ホロライブ" = ""\n', encoding="utf-8")
    g = gs.load("旧", root)
    assert g.legacy and g.notes == "说明"
    assert [(e.canonical, e.translations) for e in g.entries] == [
        ("フブキ", {"*": "吹雪"}), ("フブちゃん", {"*": "吹雪"}), ("ホロライブ", {"*": ""})]
    # 译名相同只作为合并建议，不自动视为同一实体
    assert gs.merge_suggestions(g.entries) == [{"translation": "吹雪", "lang": "*", "ids": ["フブキ", "フブちゃん"]}]
    merged = gs.merge_into(g, "フブキ", ["フブちゃん"])
    assert merged.aliases == ["フブちゃん"] and len(g.entries) == 2
    gs.save(g, root)
    assert not gs.load("旧", root).legacy and "[terms]" not in (root / "旧.toml").read_text(encoding="utf-8")


def test_language_codes_and_translation_lookup():
    assert gs.norm_lang("zh-Hans") == gs.norm_lang("zh-CN") == gs.norm_lang("zh") == "zh"
    assert gs.norm_lang("zh-Hant") == gs.norm_lang("zh-tw") == "zh-TW"
    assert gs.norm_lang("") == gs.norm_lang("*") == "*" and gs.norm_lang("EN") == "en"
    e = gs.Entry.from_dict({"canonical": "白上フブキ", "translations": {"zh-Hans": "白上吹雪", "*": "Fubuki"}})
    assert e.translation_for("zh") == "白上吹雪" and e.translation_for("zh-TW") == "Fubuki"
    assert gs.Entry(canonical="x").translation_for("zh") is None
    assert gs.Entry(canonical="x", translations={"*": ""}).translation_for("en") == ""
    with pytest.raises(gs.GlossaryError, match="重复"):
        gs.Glossary.from_dict({"name": "x", "entries": [{"canonical": "a"}, {"canonical": "a", "id": "b"}]})
    with pytest.raises(gs.GlossaryError, match="不能为空"):
        gs.Entry.from_dict({"canonical": " "})


def test_newer_format_is_rejected(tmp_path):
    root = tmp_path / "g"
    root.mkdir()
    (root / "新.toml").write_text('version = 3\nname = "新"\n', encoding="utf-8")
    with pytest.raises(gs.GlossaryError, match="升级"):
        gs.load("新", root)


def test_notes_with_triple_quotes_roundtrip():
    gs.save(gs.Glossary(name="x", notes="a \'\'\' b"))
    assert gs.load("x").notes == "a \'\'\' b"


def test_invalid_names_and_rename():
    with pytest.raises(gs.GlossaryError):
        gs.Glossary.from_dict({"name": " / "})
    assert gs.glossary_id("a/b:c") == "abc"
    with pytest.raises(gs.GlossaryError):
        gs.path_of("../evil")
    gs.save(gs.Glossary(name="old", entries=[E("a", "b")]))
    gs.save(gs.Glossary(name="new", entries=[E("a", "b")]), old_id="old")
    assert [x.id for x in gs.list_all()] == ["new"]
    gs.save(gs.Glossary(name="other"))
    with pytest.raises(gs.GlossaryError, match="已经有"):
        gs.save(gs.Glossary(name="other"), old_id="new")


def test_import_terms_formats():
    text = "# 注释\nこより = 可萝莉\n助手くん\t助手君\n\"Yakumi\",Yakumi\nホロライブ，Hololive\n"
    assert gs.import_terms(text) == {"こより": "可萝莉", "助手くん": "助手君", "Yakumi": "Yakumi",
                                     "ホロライブ": "Hololive"}


def test_resolve_merges_in_order():
    gs.save(gs.Glossary(name="a", entries=[E("x", "1"), E("y", "2", aliases=["yy"])], notes="甲"))
    gs.save(gs.Glossary(name="b", entries=[gs.Entry(canonical="y", translations={"zh": "3"})], notes="乙"))
    entries, notes = gs.resolve({"x": "0", "z": "9"}, ["a", "b"], "本期是 ASMR")
    by = {e.canonical: e for e in entries}
    assert by["x"].translations == {"*": "1"} and by["z"].translations == {"*": "9"}
    assert by["y"].translations == {"*": "2", "zh": "3"} and by["y"].aliases == ["yy"]
    assert notes == "【a】\n甲\n\n【b】\n乙\n\n【本视频】\n本期是 ASMR"
    assert gs.resolve({}, ["a"])[1] == "甲"
    with pytest.raises(gs.GlossaryError):
        gs.resolve({}, ["missing"])


def test_termset_hints_only_for_batch():
    terms = gs.TermSet([E("こより", "可萝莉", aliases=["こよ"]), E("みこ", "Miko"), E("Ina", "伊娜"),
                        E("助手くん", ""), gs.Entry(canonical="すこん部", category="fan_name")],
                       [{"sentence_id": 7, "surface": "博士小寄り", "entity": "こより"},
                        {"sentence_id": 9, "surface": "白髪", "entity": "missing"}])
    lines = terms.hints(["こよです", "Final 助手くん", "博士小寄りだよ", "すこん部"], [5, 6, 7, 8], "zh")
    assert lines == ["- こより（也写作 こよ） → 可萝莉", "- 第 3 行的「博士小寄り」指「こより」，按它翻译",
                     "- 助手くん（保留原文，不要翻译）", "- すこん部：专有名词（粉丝名），全片译法保持一致"]
    assert not any("Ina" in x or "みこ" in x for x in lines)  # Final 里的 ina 不算
    assert terms.hints(["ina!"], None, "zh") == ["- Ina → 伊娜"]
    # 原文被校对改过、片段不在了时，位置记录不再适用
    assert terms.hints(["改过的原文"], [7], "zh") == []
    assert {e.id for e in terms.used_entries()} == {"こより", "助手くん", "すこん部", "Ina"}


class Capture(Translator):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.systems = []

    def complete(self, system, user):
        self.systems.append(system)
        lines = re.findall(r"^(\d+)\|(.*)$", user.split("请翻译：", 1)[1], re.M)
        return "\n".join(f"{n}|T{t}" for n, t in lines)


class CaptureUser(Capture):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.users = []

    def complete(self, system, user):
        self.users.append(user)
        return super().complete(system, user)


def test_prompt_contains_relevant_terms_and_notes():
    tr = CaptureUser(glossary={"こより": "可萝莉", "みこ": "Miko", "助手くん": ""}, notes="主播自称こより")
    cues = [Segment(0, 1, "こよりの", group=0), Segment(1, 2, "助手くん", group=0)]
    translate_cues(tr, cues, "zh")
    system, user = tr.systems[0], tr.users[0]
    assert "本批涉及的术语" in system and "可萝莉" not in system  # 术语按批放在请求里，系统提示词不变
    assert "こより → 可萝莉" in user and "みこ" not in user and "助手くん（保留原文" in user
    assert user.index("本批涉及的术语") < user.index("请翻译：")
    assert "背景说明" in system and "主播自称こより" in system


def test_terms_are_per_batch_and_located_by_sentence():
    tr = CaptureUser(glossary=[gs.Entry(canonical="博衣こより", translations={"zh": "博衣小夜璃"})],
                     term_occurrences=[{"sentence_id": 3, "surface": "博士小寄り", "entity": "博衣こより"}],
                     concurrency=1)
    cues = [Segment(i, i + 1, text, group=i) for i, text in enumerate(
        ["こんにちは", "今日は", "雨です", "博士小寄りです", "博衣こよりでした"])]
    translate_cues(tr, cues, "zh", batch_sentences=2)
    first, second, third = tr.users
    assert "本批" not in first
    assert "第 2 行的「博士小寄り」指「博衣こより」" in second and "博衣こより → 博衣小夜璃" in second
    block = third.split("本批涉及的术语")[1].split("请翻译：")[0]
    assert "博衣こより → 博衣小夜璃" in block and "博士小寄り" not in block
    # 逐条翻译时也按句子编号匹配
    cue = CaptureUser(glossary=list(tr.terms.entries.values()), term_occurrences=[
        {"sentence_id": 3, "surface": "博士小寄り", "entity": "博衣こより"}])
    translate_cues(cue, cues, "zh", unit="cue")
    assert "第 4 行的「博士小寄り」" in cue.users[0]


def test_translation_config_errors_on_missing_glossary():
    with pytest.raises(PipelineError, match="找不到背景资料"):
        translation_config({"glossary": {}}, ["nope"], "")
    gs.save(gs.Glossary(name="g", entries=[E("a", "b"), E("c", "库")], notes="n"))
    job = ([gs.Entry(canonical="a", translations={"zh": "任务"}), gs.Entry(canonical="新", id="job:new")],
           [{"sentence_id": 1, "surface": "x", "entity": "job:new"}])
    tcfg = translation_config({"glossary": {"c": "d", "e": "f"}, "notes": "全局"}, ["g"], "本期", job)
    by = {e["canonical"]: e["translations"] for e in tcfg["glossary"]}
    # 任务审核结果 ＞ 背景资料 ＞ 配置
    assert by == {"c": {"*": "库"}, "e": {"*": "f"}, "a": {"*": "b", "zh": "任务"}, "新": {}}
    assert tcfg["term_occurrences"] == job[1]
    assert tcfg["notes"].startswith("全局") and "本期" in tcfg["notes"]


def test_job_changes_context_retranslates(fake_cfg, sample_video):
    gs.save(gs.Glossary(name="g", entries=[E("w1", "一"), E("没出现", "无")]))
    ws = fake_cfg["workspace"]["dir"]
    job = Job.create(ws, sample_video, "zh", glossaries=["g"], notes="测试")
    run(job, fake_cfg)
    used = json.loads(job.path("glossary.json").read_text())
    assert [(e["canonical"], e["used"]) for e in used["entries"]] == [("w1", True), ("没出现", False)]
    assert used["notes"] == "测试"
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
    assert "こより → 任何语言：可萝莉" in out and "VTuber 直播" in out
    res = runner.invoke(main, ["context", "set", "主播", "こより", "Koyori", "-l", "en", "-a", "こよ",
                               "--category", "person"])
    assert res.exit_code == 0 and "en：Koyori" in res.output and "又称 こよ" in res.output
    assert runner.invoke(main, ["context", "set", "主播", "こよ", "小夜璃", "-l", "zh"]).exit_code == 0  # 按别名找到实体
    assert gs.load("主播").find("こより").translations == {"*": "可萝莉", "en": "Koyori", "zh": "小夜璃"}
    assert runner.invoke(main, ["context", "set", "主播", "ジョシュ君", "助手君"]).exit_code == 0
    out = runner.invoke(main, ["context", "show", "主播"]).output
    assert "context merge 主播 助手くん ジョシュ君" in out
    assert runner.invoke(main, ["context", "merge", "主播", "助手くん", "ジョシュ君"]).exit_code == 0
    assert gs.load("主播").find("ジョシュ君").canonical == "助手くん"
    assert runner.invoke(main, ["context", "unset", "主播", "ジョシュ君"]).exit_code == 0  # 只删别名
    assert gs.load("主播").find("助手くん").aliases == []
    assert runner.invoke(main, ["context", "unset", "主播", "こより"]).exit_code == 0
    assert "VTuber 直播  · 2 条固定译法" in runner.invoke(main, ["context", "list"]).output
    assert runner.invoke(main, ["context", "delete", "主播", "--yes"]).exit_code == 0
    assert gs.list_all() == []


def test_cli_translate_with_glossary(tmp_path):
    gs.save(gs.Glossary(name="g", entries=[E("hello", "你好")]))
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
