import tomllib

import pytest

from yakumi import settings
from yakumi.config import user_config_path
from yakumi.web.app import create_app


@pytest.fixture(autouse=True)
def no_project_config(tmp_path, monkeypatch):
    """不读取仓库目录里的 yakumi.toml，也不受真实密钥环境变量影响。"""
    monkeypatch.chdir(tmp_path)
    for env in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "YAKUMI_ASR_API_KEY", "YAKUMI_TRANSLATE_API_KEY",
                "YAKUMI_TRANSLATE_MODEL", "YAKUMI_TRANSLATE_BASE_URL", "YAKUMI_ASR_BASE_URL"):
        monkeypatch.delenv(env, raising=False)


def field(data, fid):
    return next(f for g in data["groups"] for f in g["fields"] if f["id"] == fid)


def test_patch_keeps_comments_and_other_content():
    text = ('# 全局配置\n[asr]\nmodel = "a"  # 行尾注释\napi_key = "k"\n\n'
            '[translate]\n# reasoning_effort = "medium"\nstyle = """\n多行\n"""\n'
            '[translate.glossary]\n"Yakumi" = "八云"\n')
    out = settings.patch_toml(text, {"asr.model": "b#c", "asr.api_key": settings.REMOVE,
                                     "translate.style": "口语", "translate.temperature": 0.3,
                                     "subtitle.font_size": 20})
    assert "# 全局配置" in out and '# reasoning_effort = "medium"' in out
    assert 'model = "b#c"  # 行尾注释' in out
    data = tomllib.loads(out)
    assert data["asr"] == {"model": "b#c"}
    assert data["translate"] == {"style": "口语", "temperature": 0.3, "glossary": {"Yakumi": "八云"}}
    assert data["subtitle"] == {"font_size": 20}


def test_dump_round_trip():
    data = {"translate": {"model": "m", "provider_routing": {"order": ["a"], "sort": "price"},
                          "glossary": {"a b": "c"}}, "web": {"allowed_hosts": ["x.lan"]}}
    assert tomllib.loads(settings.dump_toml(data)) == data


def test_save_writes_private_file_and_describe_reports_sources():
    result = settings.save(None, {"translate.model": "deepseek/deepseek-chat", "translate.api_key": "sk-test",
                                  "subtitle.bilingual": True, "translate.temperature": "0.5"})
    path = user_config_path()
    assert result.path == path and path.stat().st_mode & 0o777 == 0o600
    assert tomllib.loads(path.read_text(encoding="utf-8"))["translate"]["temperature"] == 0.5
    data = settings.describe()
    model = field(data, "translate.model")
    assert model["value"] == "deepseek/deepseek-chat" and model["source"] == "全局配置" and model["in_target"]
    key = field(data, "translate.api_key")
    assert key["set"] and key["value"] is None and "sk-test" not in str(data)
    assert field(data, "translate.style")["source"] == "默认"
    # 删除这一项后恢复默认
    settings.save(None, {"translate.model": None})
    assert field(settings.describe(), "translate.model")["source"] == "默认"


def test_project_config_is_written_when_present(tmp_path):
    (tmp_path / "yakumi.toml").write_text("# 项目\n[translate]\nmodel = \"p\"\n", encoding="utf-8")
    settings.save(None, {"translate.model": "q"})
    assert 'model = "q"' in (tmp_path / "yakumi.toml").read_text(encoding="utf-8")
    assert not user_config_path().exists()


def test_changing_service_drops_old_key():
    settings.save(None, {"translate.api_key": "sk-or-old", "segment.api_key": "sk-seg"})
    result = settings.save(None, {"translate.base_url": "http://127.0.0.1:11434/v1"})
    data = tomllib.loads(user_config_path().read_text(encoding="utf-8"))
    # 翻译和沿用翻译地址的断句都换了服务，原来的密钥都不能再发给新地址
    assert "api_key" not in data["translate"] and "api_key" not in data["segment"]
    assert len(result.notices) == 2
    # 同时填写新密钥时保留
    settings.save(None, {"translate.base_url": "https://api.openai.com/v1", "translate.api_key": "sk-new"})
    assert tomllib.loads(user_config_path().read_text(encoding="utf-8"))["translate"]["api_key"] == "sk-new"


@pytest.mark.parametrize("changes", [
    {"asr.whisper_cpp_bin": "/bin/sh"},  # 会执行本地程序，不能在网页上改
    {"web.allowed_hosts": ["evil"]},
    {"workspace.dir": "/tmp"},
    {"translate.concurrency": 0},
    {"translate.concurrency": 1.5},
    {"translate.temperature": "abc"},
    {"translate.unit": "word"},
    {"translate.base_url": "file:///etc/passwd"},
    {"translate.model": ""},
    {"translate.model": "a\nb"},
    {"translate.api_key": "has space"},
    {"subtitle.bilingual": "yes"},
    {"asr.target_window": 600, "asr.max_window": 300},
    {},
])
def test_invalid_changes_are_rejected(changes):
    with pytest.raises(settings.SettingsError):
        settings.save(None, changes)
    assert not user_config_path().exists()


def test_env_locked_fields(monkeypatch):
    monkeypatch.setenv("YAKUMI_TRANSLATE_MODEL", "env/model")
    data = settings.describe()
    assert field(data, "translate.model")["locked"] and field(data, "translate.model")["source"].startswith("环境变量")
    with pytest.raises(settings.SettingsError):
        settings.save(None, {"translate.model": "x"})


def test_complex_file_is_rewritten_with_backup():
    path = user_config_path()
    path.parent.mkdir(parents=True)
    path.write_text('translate.model = "dotted"\n', encoding="utf-8")
    result = settings.save(None, {"translate.model": "new"})
    assert tomllib.loads(path.read_text(encoding="utf-8"))["translate"]["model"] == "new"
    assert path.with_name("config.toml.bak").read_text(encoding="utf-8") == 'translate.model = "dotted"\n'
    assert result.notices


def test_settings_api(fake_cfg):
    app = create_app(cfg=fake_cfg)
    client = app.test_client()
    page = client.get("/settings").get_data(as_text=True)
    assert "设置" in page and "settings-data" in page
    assert client.get("/api/settings").get_json()["groups"]
    resp = client.put("/api/settings", json={"changes": {"translate.batch_sentences": 9, "translate.api_key": "sk-x"}})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert "sk-x" not in resp.get_data(as_text=True) and body["path"].endswith("config.toml")
    cfg = app.extensions["yakumi_runner"].cfg
    # 新设置立即用于之后的任务，测试注入的其他配置保持不变
    assert cfg["translate"]["batch_sentences"] == 9 and cfg["translate"]["provider"] == "fake"
    assert client.put("/api/settings", json={"changes": {"translate.concurrency": -1}}).status_code == 400
    assert client.put("/api/settings", json=["x"]).status_code == 400
    assert client.put("/api/settings", json={"changes": {"a": 1}},
                      headers={"Origin": "http://evil.example"}).status_code == 403


def test_doctor_api(fake_cfg):
    client = create_app(cfg=fake_cfg).test_client()
    checks = client.get("/api/doctor").get_json()
    assert any(c["title"] == "ffmpeg" for c in checks)
    assert all(c["status"] in ("ok", "warn", "fail", "info") for c in checks)
