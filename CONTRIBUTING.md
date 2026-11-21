# 参与贡献

欢迎提交问题和改进。下面是开发时需要知道的约定。

## 开发环境

需要 Python 3.11 以上和 ffmpeg（测试会生成小视频）。

```bash
pip install -e '.[dev]'          # 想用 Silero VAD：pip install -e '.[dev,vad]'
python -m pytest -q              # 全部测试，不访问网络，不需要密钥
python -m pyflakes src tests scripts
```

提交前请确保测试和静态检查都通过，CI 会在 Python 3.11～3.14 上运行同样的检查。

先读 [`docs/architecture.md`](docs/architecture.md) 了解模块划分；涉及切分、合并、断句、翻译算法的改动，请同时看 [`docs/design/`](docs/design/) 里对应的设计记录。

## 代码约定

- **语言**：界面文字、注释、文档、提交说明都用简体中文；代码标识符用英文。
- **依赖**：核心只依赖 `click`、`httpx`、`flask` 和标准库。新增依赖前先讨论；可选功能放进 extras（例如 `vad`）。
- **模块边界**：跨模块使用的函数要有公开名字（不以 `_` 开头），不要导入其他模块的私有函数。文本拼接、中日韩判断用 `yakumi.text`；配置与缓存目录、JSON 写入用 `yakumi.files`。
- **不改动识别文字**：断句、字幕条拆分只决定“在哪里断”，文字和时间必须来自识别的词。相关改动要保持 `segment.common.check_spans` 的不变量，并在测试里检查文字拼接后不变。
- **错误**：面向用户的错误用各模块的异常类型（`MediaError`、`ASRError`、`LLMError`、`PipelineError`、`ConfigError`、`GlossaryError`），消息写清楚原因和解决办法。不要用宽泛的 `except Exception` 吞掉错误。
- **配置**：新配置项加到 `config.DEFAULTS`（附注释），同步更新 `yakumi.example.toml` 和 README。
- **流水线产物**：改变任务目录里文件的格式时，把 `models.PIPELINE_VERSION` 加一，并在 `pipeline.MIGRATIONS` 里写明旧任务从哪个阶段重跑，尽量不让用户重新识别。
- **网页**：沿用 `web/static/style.css` 里的颜色和组件变量（Yakumi 设计语言），不引入构建工具和前端框架；修改类接口要经过 `before_request` 里的跨站与 Host 检查。

## 测试

- 新功能和修复都要有测试。用假后端（`asr/fake.py`、`translate/fake.py`、`segment.llm.FakeChat`）代替真实接口。
- 涉及真实接口的验证（费用、速度、效果）写进设计文档的“实测结果”，不要写成自动测试。
- 用到 ffmpeg 的测试用 `make_video` / `sample_video` fixture 生成素材。

## 提交

- 一个提交只做一件事，说明里写清楚“为什么”。
- 用户可见的变化记录到 [`CHANGELOG.md`](CHANGELOG.md) 的“未发布”部分。
- 不要提交 `.yakumi/`、`yakumi.toml`、媒体文件和任何密钥（`.gitignore` 已经排除）。
