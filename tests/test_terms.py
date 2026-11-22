import io
import json
import time

import pytest
from click.testing import CliRunner

from yakumi import glossary as gs
from yakumi.cli import main
from yakumi.llm import LLMError
from yakumi.models import Segment
from yakumi.pipeline import Job, run
from yakumi.terms import review as rv
from yakumi.terms import scanner as sc
from yakumi.terms import sentences_from_cues
from yakumi.terms.resolver import resolve
from yakumi.terms.scanner import ScanContext, TermScanner, parse_json, plan_chunks
from yakumi.terms.validator import Candidate, SentenceText, locate, validate_item
from yakumi.web.app import create_app


def S(*texts):
    return [SentenceText(i, t, float(i * 10)) for i, t in enumerate(texts)]


# ---- 分块、解析、校验 -------------------------------------------------------
def test_plan_chunks_cover_all_with_overlap():
    sentences = S(*["あ" * 100] * 25)
    chunks = plan_chunks(sentences, chunk_chars=1000, overlap_chars=200)
    assert chunks[0] == (0, 10) and chunks[1][0] == 8  # 与上一块重叠两句（200 字）
    assert chunks[-1][1] == 25
    assert all(a[1] > b[0] for a, b in zip(chunks, chunks[1:]))  # 相邻两块有重叠、没有空隙
    assert plan_chunks(S("x" * 5000), 1000) == [(0, 1)]  # 超长的一句自成一块
    assert plan_chunks([], 1000) == []


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('好的：\n```json\n{"terms": [{"a": 1}]}\n```', "terms") == [{"a": 1}]
    assert parse_json('[{"a": 1}]', "terms") == [{"a": 1}]
    with pytest.raises(ValueError):
        parse_json("没有结果", "terms")


def test_locate_and_validate():
    assert locate("ＫＯＹＯＲＩ", "今日は koyori です") == "koyori"
    assert locate("博衣 こより", "博衣こよりだよ") == "博衣こより"
    assert locate("みこ", "こより") is None
    sents = {s.id: s for s in S("今日はフブちゃんと", "博士小寄りです", "ＡＢＣ")}
    ok, _ = validate_item({"sentence_id": 1, "surface": "博士小寄り", "canonical": "null", "translation": "",
                           "category": "PERSON", "confidence": "sure"}, sents, [0, 1, 2])
    assert (ok.surface, ok.canonical, ok.translation, ok.category, ok.confidence) == \
        ("博士小寄り", None, None, "person", "low")
    moved, _ = validate_item({"sentence_id": 1, "surface": "フブちゃん"}, sents, [0, 1, 2])
    assert moved.sentence_id == 0 and moved.fixed == "moved"
    norm, _ = validate_item({"sentence_id": 2, "surface": "abc"}, sents, [0, 1, 2])
    assert norm.surface == "ＡＢＣ" and norm.fixed == "normalized"
    for bad, reason in [({"sentence_id": 1, "surface": "不存在"}, "找不到"), ({"sentence_id": 1}, "缺少"),
                        ("x", "不是对象"), ({"sentence_id": 1, "surface": "长" * 50}, "太长")]:
        assert validate_item(bad, sents, [0, 1, 2])[0] is None
        assert reason in validate_item(bad, sents, [0, 1, 2])[1]


class Scripted:
    """按顺序返回预先写好的回复，记录收到的请求。"""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)


def test_scanner_repairs_invalid_surface_once():
    sentences = S("こんこよ", "博士小寄りが来た", "白上さん")
    chat = Scripted(
        {"terms": [{"sentence_id": 1, "surface": "博士小寄", "canonical": "博衣こより", "confidence": "high"},
                   {"sentence_id": 1, "surface": "ハカセコヨリ"}, {"sentence_id": 9, "surface": "幽霊"}]},
        {"fixes": [{"index": 1, "surface": "博士小寄り"}, {"index": 2, "surface": None}]},
    )
    ctx = ScanContext(target="zh", title="【ASMR】はじめての", notes="博衣こより的直播",
                      known=[gs.Entry(canonical="白上フブキ", aliases=["白上"], translations={"zh": "白上吹雪"}),
                             gs.Entry(canonical="無関係")])
    result = TermScanner(chat).scan(sentences, ctx)
    user = chat.calls[0][1]
    assert "视频标题：【ASMR】はじめての" in user and "博衣こより的直播" in user
    assert user.index("白上フブキ（又称 白上） → 白上吹雪") < user.index("無関係")  # 这一块里出现的排在前面
    assert "1|博士小寄りが来た" in user and "简体中文" in chat.calls[0][0]
    # “博士小寄” 是原文里的片段，直接有效；“ハカセコヨリ” 经过一次定向修复；编号 9 不存在，不发修复请求
    assert [(c.sentence_id, c.surface, c.fixed) for c in result.candidates] == [
        (1, "博士小寄", ""), (1, "博士小寄り", "repaired")]
    assert len(chat.calls) == 2 and "ハカセコヨリ" in chat.calls[1][1] and "幽霊" not in chat.calls[1][1]
    assert [x["reason"] for x in result.invalid] == ["句子编号不存在"] and result.repairs == 1
    # 修复后仍找不到：保留为异常记录
    chat = Scripted({"terms": [{"sentence_id": 0, "surface": "ないもの"}]}, {"fixes": [{"index": 1, "surface": None}]})
    result = TermScanner(chat).scan(sentences, ScanContext("zh"))
    assert result.candidates == [] and result.invalid[0]["reason"] == "修复后仍找不到"


def test_scanner_tolerates_failed_chunks_but_not_all():
    sentences = S(*["あ" * 300] * 4)
    chat = Scripted({"terms": []}, LLMError("超时"))
    result = TermScanner(chat, chunk_chars=500, overlap_chars=0, concurrency=1).scan(sentences, ScanContext("zh"))
    assert result.chunks == 2 and result.failed_chunks == 1 and "扫描失败" in result.invalid[0]["reason"]
    with pytest.raises(LLMError):
        TermScanner(Scripted("不是 JSON", "还不是")).scan(S("a"), ScanContext("zh"))


# ---- 关联 -----------------------------------------------------------------
def cand(sid, surface, canonical=None, translation=None, confidence="medium", category="person"):
    return Candidate(sid, surface, canonical, translation, category, confidence, "依据")


def test_resolver_links_library_and_merges_aliases():
    sentences = S("フブちゃんこんにちは", "博士小寄りです", "こよりだよ", "こよりと博士小寄り", "すこんぶ", "白髪のこより")
    library = [gs.Entry(canonical="白上フブキ", aliases=["フブちゃん"], translations={"zh": "白上吹雪"}, id="fbk"),
               gs.Entry(canonical="博衣こより", aliases=["こより"], translations={"*": "博衣小夜璃"})]
    entities = resolve([cand(0, "フブちゃん"), cand(1, "博士小寄り", "博衣こより", "博衣可萝莉", "low"),
                        cand(2, "こより"), cand(4, "すこんぶ", "すこん部", "", category="fan_name"),
                        cand(5, "白髪", None, None, "low")],
                       sentences, library, "zh")
    by = {e.key: e for e in entities}
    fbk = by["lib:fbk"]
    assert fbk.status == "approved" and fbk.auto and fbk.translation == "白上吹雪"  # 名称、别名逐字对应
    koyori = by["lib:博衣こより"]
    # 模型给出的规范名称对应到已有实体；有一个片段不是已确认的称呼，所以需要人工审核
    assert koyori.status == "pending" and not koyori.auto and koyori.translation == "博衣小夜璃"
    assert koyori.surfaces == ["博士小寄り", "こより"] and koyori.confidence == "medium"
    sites = [(o.sentence_id, o.surface, o.found_by) for o in koyori.occurrences]
    assert sites == [(1, "博士小寄り", "model"), (2, "こより", "model"), (3, "博士小寄り", "search"),
                     (3, "こより", "search"), (5, "こより", "search")]
    assert by["new:すこん部"].canonical == "すこん部" and by["new:すこん部"].translation is None
    assert by["new:白髪"].canonical is None and by["new:白髪"].status == "pending"


def test_resolver_skips_spans_inside_longer_surfaces():
    sentences = S("博衣こよりです", "こよりです")
    entities = resolve([cand(0, "博衣こより", "博衣こより"), cand(1, "こより", "こより")], sentences, [], "zh")
    by = {e.key: [(o.sentence_id, o.surface) for o in e.occurrences] for e in entities}
    assert by == {"new:博衣こより": [(0, "博衣こより")], "new:こより": [(1, "こより")]}


def test_sentences_from_cues_join_groups():
    cues = [Segment(0, 1, "博士", group=0), Segment(1, 2, "小寄り", group=0), Segment(2, 3, "x", group=-1),
            Segment(3, 4, "です", group=1)]
    assert [(s.id, s.text, s.start, s.end) for s in sentences_from_cues(cues)] == [
        (0, "博士小寄り", 0, 2), (1, "です", 3, 4)]


# ---- 审核数据 ---------------------------------------------------------------
def make_review():
    sentences = S("博士小寄りです", "こよりだよ", "白髪のこより", "白髪")
    entities = resolve([cand(0, "博士小寄り", "博衣こより", "博衣小夜璃"), cand(1, "こより", "博衣こより"),
                        cand(3, "白髪", None, None, "low")], sentences, [], "zh")
    return rv.Review(target="zh", entities=entities)


def test_review_updates_merge_and_export():
    review = make_review()
    key = "new:博衣こより"
    with pytest.raises(rv.ReviewError):
        rv.apply_update(review, {"key": key, "status": "ok"})
    with pytest.raises(rv.ReviewError):
        rv.apply_update(review, {"key": "nope"})
    with pytest.raises(rv.ReviewError):
        rv.apply_update(review, {"key": key, "category": "unknown"})
    e = rv.apply_update(review, {"key": key, "status": "approved", "translation": " 小夜璃 ", "aliases": "こよ、こより",
                                 "occurrences": [{"sentence_id": 2, "surface": "こより", "status": "rejected"}]})
    assert e.translation == "小夜璃" and e.edited and e.aliases == ["こよ", "こより"]
    assert rv.approved_terms(review) == (
        [gs.Entry(canonical="博衣こより", id="job:" + key, category="person", aliases=["こよ", "こより"],
                  translations={"zh": "小夜璃"})],
        [{"sentence_id": 0, "surface": "博士小寄り", "entity": "job:" + key},
         {"sentence_id": 1, "surface": "こより", "entity": "job:" + key}])
    merged = rv.merge(review, key, ["new:白髪"])
    assert len(review.entities) == 1 and "白髪" in merged.surfaces
    assert [(o.sentence_id, o.surface) for o in merged.occurrences] == [
        (0, "博士小寄り"), (1, "こより"), (2, "こより"), (2, "白髪"), (3, "白髪")]
    assert rv.Review.from_dict(json.loads(json.dumps(review.to_dict()))).to_dict() == review.to_dict()


def test_auto_approve_only_library_links():
    sentences = S("フブちゃん", "白上フブさん", "新人さん")
    library = [gs.Entry(canonical="白上フブキ", aliases=["フブちゃん"])]
    review = rv.Review("zh", resolve([cand(0, "フブちゃん"), cand(1, "白上フブ", "白上フブキ"),
                                      cand(2, "新人", "新人", confidence="high")], sentences, library, "zh"))
    # “白上フブ” 由模型对应到已有实体，置信度不低，自动通过；新发现的“新人”不采用
    assert rv.auto_approve(review) == 1 and review.finished
    assert {e.key: e.status for e in review.entities} == {"lib:白上フブキ": "approved", "new:新人": "pending"}


def test_carry_over_keeps_decisions():
    old = make_review()
    rv.apply_update(old, {"key": "new:白髪", "status": "rejected"})
    rv.apply_update(old, {"key": "new:博衣こより", "translation": "可萝莉",
                          "occurrences": [{"sentence_id": 1, "surface": "こより", "status": "rejected"}]})
    new = make_review()
    rv.carry_over(new, old)
    by = {e.key: e for e in new.entities}
    assert by["new:白髪"].status == "rejected"
    assert by["new:博衣こより"].translation == "可萝莉" and by["new:博衣こより"].status == "pending"
    assert [o.status for o in by["new:博衣こより"].occurrences if o.sentence_id == 1] == ["rejected"]


def test_save_to_library_keeps_asr_errors_out():
    review = make_review()
    e = review.find("new:博衣こより")
    with pytest.raises(rv.ReviewError, match="还没有通过"):
        rv.save_to_library(e, "主播", "zh")
    gs.save(gs.Glossary(name="主播", entries=[gs.Entry(canonical="博衣こより", translations={"en": "Koyori"})]))
    rv.apply_update(review, {"key": e.key, "status": "approved", "aliases": ["こより"]})
    rv.save_to_library(e, "主播", "zh-Hans")
    [entry] = gs.load("主播").entries
    assert entry.translations == {"en": "Koyori", "zh": "博衣小夜璃"} and entry.aliases == ["こより"]
    assert "博士小寄り" not in entry.surfaces() and e.saved_to == ["主播"]


# ---- 流水线 -----------------------------------------------------------------
SRT_CUES = ["やあ Koyori です。", "今日は Koyori の配信。", "Fubuki も来た。", "おつこよ。"]


def subtitle_job(cfg, term_mode="review", glossaries=None):
    job = Job.create(cfg["workspace"]["dir"], None, "zh", term_mode=term_mode, glossaries=glossaries)
    job.import_subtitles([Segment(i, i + 1, t) for i, t in enumerate(SRT_CUES)])
    return job


def test_pipeline_pauses_for_review_then_translates(fake_cfg):
    job = subtitle_job(fake_cfg)
    state = run(job, fake_cfg)
    assert state.status == "awaiting_review" and state.stage == "term_review"
    assert "term_scan" in state.completed and "translate" not in state.completed
    assert "2 个待审核" in state.message
    review = rv.load_review(job.dir)
    assert [e.surfaces for e in review.entities] == [["Koyori"], ["Fubuki"]]
    assert json.loads(job.path(rv.CANDIDATES_FILE).read_text())["chunks"] == 1
    # 重新执行：审核没完成，仍然停在审核
    assert run(job, fake_cfg).status == "awaiting_review"
    rv.apply_update(review, {"key": review.entities[0].key, "status": "approved", "canonical": "博衣こより",
                             "translation": "博衣小夜璃"})
    review.finished = True  # Fubuki 仍待审核，不使用
    rv.save_review(job.dir, review)
    state = run(job, fake_cfg)
    assert state.status == "done"
    used = json.loads(job.path("glossary.json").read_text())
    assert [(e["canonical"], e["used"]) for e in used["entries"]] == [("博衣こより", True)]
    assert {o["sentence_id"] for o in used["occurrences"]} == {0, 1}
    assert state.stats["term_entities"] == 2


def test_pipeline_auto_mode_and_off(fake_cfg):
    gs.save(gs.Glossary(name="g", entries=[gs.Entry(canonical="Fubuki", translations={"*": "吹雪"})]))
    job = subtitle_job(fake_cfg, "auto", ["g"])
    assert run(job, fake_cfg).status == "done"
    review = rv.load_review(job.dir)
    assert {e.surfaces[0]: e.status for e in review.entities} == {"Koyori": "pending", "Fubuki": "approved"}
    off = subtitle_job(fake_cfg, "off")
    assert run(off, fake_cfg).status == "done" and not off.path(rv.REVIEW_FILE).exists()
    assert "term_scan" not in off.state.completed


def test_rescan_keeps_decisions(fake_cfg):
    job = subtitle_job(fake_cfg)
    run(job, fake_cfg)
    review = rv.load_review(job.dir)
    rv.apply_update(review, {"key": review.entities[1].key, "status": "rejected"})
    rv.save_review(job.dir, review)
    run(job, fake_cfg, from_stage="term_scan")
    assert [e.status for e in rv.load_review(job.dir).entities] == ["pending", "rejected"]


# ---- 网页 -----------------------------------------------------------------
def wait_idle(app, client, job_id, timeout=30):
    runner = app.extensions["yakumi_runner"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not runner.busy(job_id):
            return client.get(f"/api/jobs/{job_id}").get_json()
        time.sleep(0.1)
    raise AssertionError("任务超时")


def srt_upload():
    body = "\n".join(f"{i + 1}\n00:00:0{i},000 --> 00:00:0{i},900\n{t}\n" for i, t in enumerate(SRT_CUES))
    return io.BytesIO(body.encode()), "talk.srt"


def test_web_review_flow(fake_cfg):
    gs.save(gs.Glossary(name="主播"))
    app = create_app(cfg=fake_cfg)
    client = app.test_client()
    assert client.post("/api/jobs", data={"mode": "subtitles", "target": "zh", "terms": "bad",
                                          "subtitles": srt_upload()}).status_code == 400
    resp = client.post("/api/jobs", data={"mode": "subtitles", "target": "zh", "terms": "review",
                                          "subtitles": srt_upload()})
    job_id = resp.get_json()["id"]
    state = wait_idle(app, client, job_id)
    assert state["status"] == "awaiting_review" and state["status_label"] == "待审核"
    assert state["terms"]["pending"] == 2
    assert "术语审核" in client.get(f"/jobs/{job_id}").get_data(as_text=True)
    data = client.get(f"/api/jobs/{job_id}/terms").get_json()
    koyori, fubuki = data["review"]["entities"]
    assert data["sentences"]["0"]["text"] == "やあ Koyori です。" and data["glossaries"][0]["id"] == "主播"
    rev = state["revision"]
    assert client.post(f"/api/jobs/{job_id}/terms", json={"revision": rev - 1}).status_code == 409
    bad = client.post(f"/api/jobs/{job_id}/terms", json={"revision": rev, "updates": [{"key": "x"}]})
    assert bad.status_code == 400
    saved = client.post(f"/api/jobs/{job_id}/terms", json={
        "revision": rev, "action": "save",
        "updates": [{"key": koyori["key"], "status": "approved", "canonical": "博衣こより", "translation": "小夜璃"}],
        "save_to": [{"key": koyori["key"], "glossary": "主播"}]})
    assert saved.status_code == 200 and saved.get_json()["status"] == "awaiting_review"
    assert gs.load("主播").entries[0].canonical == "博衣こより"
    resp = client.post(f"/api/jobs/{job_id}/terms", json={
        "revision": saved.get_json()["revision"], "action": "continue",
        "updates": [{"key": fubuki["key"], "status": "rejected"}]})
    assert resp.status_code == 200
    state = wait_idle(app, client, job_id)
    assert state["status"] == "done" and state["terms"]["finished"]
    # 审核结果改了之后，“保存并重新翻译”从翻译开始
    resp = client.post(f"/api/jobs/{job_id}/terms", json={
        "revision": state["revision"], "action": "continue",
        "updates": [{"key": fubuki["key"], "status": "approved", "translation": "吹雪"}]})
    state = wait_idle(app, client, job_id)
    assert state["status"] == "done"
    used = json.loads(Job.load(fake_cfg["workspace"]["dir"], job_id).path("glossary.json").read_text())
    assert {e["canonical"] for e in used["entries"]} == {"博衣こより", "Fubuki"}


def test_web_scan_existing_job(fake_cfg):
    app = create_app(cfg=fake_cfg)
    client = app.test_client()
    job_id = client.post("/api/jobs", data={"mode": "subtitles", "target": "zh",
                                            "subtitles": srt_upload()}).get_json()["id"]
    state = wait_idle(app, client, job_id)
    assert state["status"] == "done" and state["terms"] is None and state["term_mode"] == "off"
    assert client.get(f"/api/jobs/{job_id}/terms").get_json()["review"] is None
    assert client.post(f"/api/jobs/{job_id}/terms", json={"action": "save"}).status_code == 400
    resp = client.post(f"/api/jobs/{job_id}/terms", json={"revision": state["revision"], "action": "scan"})
    assert resp.status_code == 200
    state = wait_idle(app, client, job_id)
    assert state["status"] == "awaiting_review" and state["term_mode"] == "review"
    # 不再使用术语扫描：停在审核的任务继续处理
    resp = client.post(f"/api/jobs/{job_id}/terms", json={"revision": state["revision"], "action": "disable"})
    assert resp.status_code == 200
    state = wait_idle(app, client, job_id)
    assert state["status"] == "done" and state["term_mode"] == "off" and state["terms"] is None


# ---- 命令行 -----------------------------------------------------------------
def test_cli_terms_commands(fake_cfg, monkeypatch, tmp_path):
    job = subtitle_job(fake_cfg)
    run(job, fake_cfg)
    monkeypatch.chdir(tmp_path)
    conf = tmp_path / "c.toml"
    conf.write_text(f'[workspace]\ndir = "{fake_cfg["workspace"]["dir"]}"\n', encoding="utf-8")
    runner = CliRunner()

    def cli(*args, **kw):
        res = runner.invoke(main, ["-c", str(conf), "terms", *args], **kw)
        assert res.exit_code == 0, res.output
        return res.output

    out = cli("show", job.state.id, "--all")
    assert "共 2 个术语：待审核 2" in out and "1. [待审核] Koyori" in out and "今日は" in out
    gs.save(gs.Glossary(name="主播"))
    out = cli("set", job.state.id, "1", "--approve", "--canonical", "博衣こより", "--translation", "小夜璃",
              "--save-to", "主播")
    assert "[通过] Koyori → 博衣こより → 小夜璃" in out and "已保存到背景资料" in out
    assert gs.load("主播").find("博衣こより") is not None
    out = cli("review", job.state.id, input="r\ny\n")
    assert "审核已结束" in out
    review = rv.load_review(job.dir)
    assert review.finished and [e.status for e in review.entities] == ["approved", "rejected"]
    res = runner.invoke(main, ["-c", str(conf), "terms", "show", "nope"])
    assert res.exit_code != 0


def test_cli_run_pauses_and_resumes(make_video, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "yakumi.toml").write_text('[chunking]\nvad = "energy"\n[segment]\nmode = "rules"\n', encoding="utf-8")
    monkeypatch.setattr(sc.FakeTermChat, "complete", lambda self, system, user: json.dumps(
        {"terms": [{"sentence_id": 0, "surface": "w1", "canonical": "W1", "category": "other"}]}))
    video = make_video(5)
    runner = CliRunner()
    args = ["run", str(video), "-t", "zh", "--asr", "fake", "--translator", "fake", "--terms", "review"]
    res = runner.invoke(main, args)
    assert res.exit_code == 0, res.output
    assert "yakumi terms review" in res.output and not (tmp_path / "sample.zh.srt").exists()
    [state] = Job.list(tmp_path / ".yakumi")
    assert state.status == "awaiting_review"
    assert runner.invoke(main, ["terms", "set", state.id, "1", "--approve", "--translation", "一"]).exit_code == 0
    assert runner.invoke(main, ["terms", "done", state.id]).exit_code == 0
    res = runner.invoke(main, args)
    assert res.exit_code == 0, res.output
    assert (tmp_path / "sample.zh.srt").is_file()
    job = Job.load(tmp_path / ".yakumi", state.id)
    assert job.state.status == "done" and job.state.stats["asr_requests"] == 1
    # 改了审核结果，再次执行时只重新翻译
    assert runner.invoke(main, ["terms", "set", state.id, "1", "--translation", "壹"]).exit_code == 0
    res = runner.invoke(main, args)
    assert "从「翻译字幕」" in res.output
    assert "壹" in job.path("glossary.json").read_text(encoding="utf-8")


# ---- 回归 -----------------------------------------------------------------
def test_reviewed_variant_linked_to_library_entity_keeps_location():
    from yakumi.pipeline import translation_config
    gs.save(gs.Glossary(name="g", entries=[gs.Entry(canonical="博衣こより", id="koyori", translations={"zh": "小夜璃"})]))
    review = rv.Review("zh", resolve([cand(0, "Koyori"), cand(1, "新人", "新人"), cand(2, "新人さん", "新人2")],
                                     S("やあ koyori です", "新人です", "新人さんです"), [], "zh"))
    for key, canonical in (("new:koyori", "博衣こより"), ("new:新人", "同名"), ("new:新人2", "同名")):
        rv.apply_update(review, {"key": key, "status": "approved", "canonical": canonical})
    tcfg = translation_config({"glossary": {}}, ["g"], "", rv.approved_terms(review))
    terms = gs.TermSet(gs.entries_from_any(tcfg["glossary"]), tcfg["term_occurrences"])
    assert terms.hints(["やあ koyori です"], [0], "zh") == ["- 博衣こより → 小夜璃",
                                                       "- 第 1 行的「koyori」指「博衣こより」，按它翻译"]
    assert any("「新人さん」指「同名」" in x for x in terms.hints(["新人さんです"], [2], "zh"))


def test_legacy_keys_differing_only_by_width_still_load(tmp_path):
    (gs.glossaries_dir()).mkdir(parents=True, exist_ok=True)
    (gs.glossaries_dir() / "旧.toml").write_text('name = "旧"\n[terms]\n"Koyori" = "a"\n"koyori" = "b"\n', encoding="utf-8")
    assert len(gs.load("旧").entries) == 2 and [g.id for g in gs.list_all()] == ["旧"]


def test_occurrence_rejection_survives_rescan_and_alias_newline_rejected():
    old = make_review()
    rv.apply_update(old, {"key": "new:博衣こより", "occurrences": [{"sentence_id": 1, "surface": "こより", "status": "rejected"}]})
    new = make_review()
    rv.carry_over(new, old)
    assert [o.status for o in new.find("new:博衣こより").occurrences if o.sentence_id == 1] == ["rejected"]
    with pytest.raises(rv.ReviewError):
        rv.apply_update(new, {"key": "new:白髪", "aliases": ["a\nb"]})


def test_web_disable_on_finished_job_is_not_busy(fake_cfg):
    app = create_app(cfg=fake_cfg)
    client = app.test_client()
    job_id = client.post("/api/jobs", data={"mode": "subtitles", "target": "zh", "terms": "auto",
                                            "subtitles": srt_upload()}).get_json()["id"]
    state = wait_idle(app, client, job_id)
    assert state["status"] == "done"
    resp = client.post(f"/api/jobs/{job_id}/terms", json={"revision": state["revision"], "action": "disable"})
    assert resp.get_json()["busy"] is False and resp.get_json()["term_mode"] == "off"


def test_cli_auto_mode_does_not_retranslate_on_rerun(make_video, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "yakumi.toml").write_text('[chunking]\nvad = "energy"\n[segment]\nmode = "rules"\n', encoding="utf-8")
    gs.save(gs.Glossary(name="g", entries=[gs.Entry(canonical="W1", translations={"*": "一"})]))
    monkeypatch.setattr(sc.FakeTermChat, "complete", lambda self, system, user: json.dumps(
        {"terms": [{"sentence_id": 0, "surface": "w1", "canonical": "W1", "confidence": "high"}]}))
    args = ["run", str(make_video(5)), "-t", "zh", "--asr", "fake", "--translator", "fake", "--terms", "auto", "-x", "g"]
    runner = CliRunner()
    assert runner.invoke(main, args).exit_code == 0
    res = runner.invoke(main, args)
    assert res.exit_code == 0 and "重新处理" not in res.output
