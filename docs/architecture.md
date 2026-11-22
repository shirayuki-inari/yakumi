# 架构概览

这份文档面向想读代码或参与开发的人：说明 Yakumi 由哪些部分组成、数据怎么流动、扩展点在哪里。
具体算法的设计取舍见 [`docs/design/`](design/)。

## 总体结构

```
                ┌──────────── cli.py（命令行）
                │
用户 ───────────┼──────────── web/app.py（网页工作台，Flask，单线程任务队列）
                │
                ▼
          pipeline.py  ──  任务目录 ~/.local/share/yakumi/jobs/<id>/（每个阶段的产物都落盘，可续跑）
                │
   ┌────────────┼──────────────┬──────────────┬──────────────┐
   ▼            ▼              ▼              ▼              ▼
 media.py    chunking/       segment/      translate/     subtitle/
 (ffmpeg)    长音频切分识别   LLM 断句       整句翻译        格式与标点
               │                │              │
               ▼                └──────┬───────┘
             asr/                      ▼
           识别后端                  llm.py（OpenAI 兼容对话接口）
```

命令行和网页只负责收集参数、展示进度，所有处理都在 `pipeline.run()` 里完成。

## 流水线

| 阶段 | 实现 | 输入 → 输出 |
| --- | --- | --- |
| extract | `media.extract_audio` | 视频 → `audio.wav`（16kHz 单声道 16 位） |
| vad | `chunking/vad.py` | → `vad.json`：语音区间（Silero，或 ffmpeg 静音检测） |
| transcribe | `chunking/transcribe.py` + `chunking/planner.py` | → `windows.json`、`pieces/`：按停顿分窗、逐窗识别，每窗单独缓存 |
| merge | `chunking/merge.py` | → `merged.json`：在重叠区按时间约束做 LCS，拼成全局词序列 |
| repair | `chunking/repair.py`（+ `align/` 强制对齐接口） | → `words.json`、`repair.json`：切点附近有问题时截取局部音频重识别 |
| segment | `segment/` | → `sentences.json`（句子）、`aligned.json`（字幕条，带 `group`） |
| term_scan | `terms/`（可选） | → `term_candidates.json`、`term_review.json`：分块扫描术语、校验原文片段、关联实体 |
| term_review | `terms/review.py`（可选） | 等待人工审核；没审核完时任务状态为 `awaiting_review`，审核完成后再次执行即继续 |
| translate | `translate/grouped.py` | → `translated.json`、`glossary.json`（任务级术语表）：整句翻译，再按字幕条分段 |
| write | `subtitle/formats.py` + `subtitle/style.py` | → `subtitles.<格式>`、`preview.vtt` |
| burn | `media.burn_subtitles` | → `output.mp4` |

- 已完成的阶段记录在 `state.json` 的 `completed` 里，再次运行时跳过；`Job.invalidate_from(stage)` 让某个阶段及之后的阶段重跑。
- 术语扫描的两个阶段只在任务的 `term_mode` 为 `review` / `auto` 且需要翻译时启用。`term_review` 遇到未完成的审核时抛出 `pipeline.AwaitingReview`，`run()` 把任务记为 `awaiting_review` 并正常返回（不是错误），网页队列随即空出；修改审核结果只让翻译及之后的阶段重跑，不会重新识别。
- 产物格式变化时把 `models.PIPELINE_VERSION` 加一，并在 `pipeline.MIGRATIONS` 里写明旧版本从哪个阶段开始重跑，避免用户重新识别（识别是最慢、最贵的一步）。
- `audio.wav` 只在 vad / transcribe / repair 阶段需要，任务完成后删除；之后需要时会自动重新抽取。

## 两层数据：句子与字幕条

- **词**（`asr.types.Word`）：识别结果的最小单位，带时间戳。断句和字幕条的文字、时间都只来自词，从不改写。
- **句子**（`segment.common.Sentence`）：翻译单位，对应一段词区间 `[i, j)`。
- **字幕条**（`models.Segment`）：显示单位，属于某个句子（`group`）。网页编辑、导出都以字幕条为单位。

断句（模型只插入换行）和整句翻译（译文按 `¦` 分段）都有逐级兜底，保证任何情况下字幕条与时间轴一一对应。

## 目录与模块

| 模块 | 职责 |
| --- | --- |
| `cli.py` | click 命令：run / transcribe / translate / burn / serve / passwd / jobs / doctor / context / terms |
| `web/` | 网页工作台：`app.py`（路由、任务队列、跨站与 Host 检查）、`templates/`、`static/` |
| `settings.py` | 网页可修改的配置项（分组、校验、来源），按行写回配置文件并保留注释 |
| `doctor.py` | 环境与配置检查，命令行 `doctor` 和网页设置页共用 |
| `download.py` | 视频链接下载（可选，调用 yt-dlp 子进程），以及链接地址检查 |
| `web/auth.py` | 网页登录：密码校验、登录失败限速、签名密钥 |
| `web/browse.py` | 浏览和选择服务器上允许访问的目录里的文件 |
| `pipeline.py` | 任务（`Job`）的创建、载入、迁移、续跑，以及各阶段函数 |
| `config.py` | 默认配置、配置文件与环境变量的合并（`build_config`）、未知配置项提示 |
| `models.py` | `Segment`、`JobState`、阶段列表与流水线版本 |
| `files.py` | 用户配置与缓存目录（XDG）、JSON 原子写入 |
| `text.py` | 中日韩文字判断、按语言拼接、字幕字数上限 |
| `media.py` | ffmpeg / ffprobe 封装：抽音频、切片段、探测、烧录 |
| `asr/` | 识别后端：`openai_compat`（默认，OpenRouter 等）、`whisper_cpp`（本地）、`fake`（测试） |
| `chunking/` | 长音频：VAD、窗口规划、分窗识别、重叠合并、边界复核 |
| `align/` | 强制对齐后端的接口（目前只有不做对齐的 `none`） |
| `llm.py` | OpenAI 兼容对话客户端：重试、思考档位、OpenRouter 路由、用量统计 |
| `segment/` | 断句：`llm.py`（LLM 断句与校验映射）、`rules.py`（规则兜底）、`cues.py`（句子 → 字幕条）、`timing.py`（纯文本模式的时间估算） |
| `translate/` | 翻译：`base.py`（分批、编号、兜底）、`grouped.py`（整句翻译）、`openai_compat.py`、`fake.py` |
| `glossary.py` | 背景资料（背景说明 + 可选的固定译法），保存在 `~/.config/yakumi/glossaries/`。界面和命令行叫“背景资料” / `context`，代码、文件和配置键沿用 glossary。固定译法以实体（`Entry`：规范名称、别名、类别、按语言的译名）为单位，兼容读取旧的 `[terms]`；`merge_entries` 按优先级合并，`TermSet` 为每一批翻译挑出相关的实体和审核确认的片段 |
| `terms/` | 术语扫描与审核：`scanner.py`（分块、调用模型、提取候选）、`validator.py`（结构和原文定位校验）、`resolver.py`（去重、别名及实体关联）、`review.py`（审核数据、持久化、用于翻译、保存到背景资料） |
| `subtitle/` | `formats.py`（SRT / VTT / ASS 读写）、`style.py`（中日文字幕标点惯例） |
| `stats.py` | 把任务统计整理成一行摘要（请求数、兜底次数、费用） |

## 扩展点

- **识别后端**：实现 `asr.base.ASRProvider`（`name`、`mode`、`capabilities()`、`transcribe()`），在 `asr/__init__.py` 的 `create_asr` 里注册。`capabilities()` 声明是否有词级时间戳、单次请求的窗口上限，分窗逻辑据此自动调整。
- **翻译后端**：继承 `translate.base.Translator`，只需实现 `complete(system, user)`；分批、编号校验、整句翻译、按批附加术语和兜底都是公共逻辑。
- **断句**：`segment.create_segmenter` 返回带 `split(words) -> list[Span]` 的对象。任何实现都必须满足“区间首尾相接、覆盖全部词”（`segment.common.check_spans`）。
- **强制对齐**：实现 `align.Aligner`，返回与输入一一对应的词（`align.check_alignment` 会校验）。
- **字幕格式**：在 `subtitle/formats.py` 的 `render` 里加分支，并加入 `FORMATS`。

## 并发与状态

- 识别窗口串行请求（上游有超时限制，需要按结果决定是否拆分）；断句和翻译默认 4 路并发（`ThreadPoolExecutor`），用量统计加锁。
- 每个网页进程有一个单线程任务队列。校对、重新翻译以及 CLI 运行会先为任务取得文件锁，网页将锁持有至后台处理结束。网页带着读取时的任务版本提交编辑，版本过期时会收到 409。
- 任务状态来自 `state.json`。JSON 产物先写入各自的临时文件，再原子替换目标文件。

## 测试

- 测试不访问网络：`asr/fake.py` 按绝对时间生成确定的词（`w0 w1 …`），可以精确验证切分与合并；`translate/fake.py` 和 `segment.llm.FakeChat` 让整条流水线离线跑通。
- `tests/conftest.py` 把 `XDG_CONFIG_HOME`、`XDG_CACHE_HOME` 指向临时目录，测试不会读写真实的配置、背景资料和缓存。
- 需要 ffmpeg 的测试在找不到 ffmpeg 时自动跳过；Silero VAD 的测试在没有 onnxruntime 时跳过。
