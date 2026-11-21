# 长音频切分与转录合并：设计记录

> **状态：已实现（0.1.0）。** 这是实施前写的设计与计划，保留下来说明各项取舍；“背景”里的“现在的做法”指当时的旧实现。
> 代码以 `src/yakumi/chunking/` 为准，模块说明见 [`docs/architecture.md`](../architecture.md)，实测数据见文末。

## 背景

当时的做法：音频超过 24MB（48kbps 下约 70 分钟）时，每 10 分钟硬切一刀，各段独立识别后按时间偏移直接拼接。问题有：

- 切点可能落在说话中间，导致漏字、重复；
- 拼接时不做任何校验；
- 切分逻辑和 OpenAI 的 25MB 文件限制绑死。

默认识别后端改为 **OpenRouter 上的 Gemini 3.5 Transcribe**（`google/gemini-3.5-transcribe`），通过 OpenRouter 的 OpenAI 兼容接口 `POST https://openrouter.ai/api/v1/audio/transcriptions` 调用。它的特点和限制：

- 支持 multipart 上传（字段与 OpenAI 相同），单个文件**上限 25MB**；
- **上游请求 60 秒超时**。按 OpenRouter 文档，处理时间超过约 1 分钟的录音应当切分。这是窗口长度的主要约束，比文件大小更紧；
- `response_format=verbose_json` 加 `timestamp_granularities[]=word` 可以请求词级时间戳。但**文档没有说明 Gemini 这个模型是否支持**，有些模型会直接返回 400；
- `language` 参数使用 ISO-639-1 代码，不传则自动检测；返回结果里的 `language`、`confidence` 只在后端提供时才有；
- 返回 `usage.cost`（美元），可以直接统计开销；
- 上游 Google 还有一个限制：开启词级时间戳时单次最长 30 分钟，超出部分被静默截断。因为有 60 秒超时，窗口远小于 30 分钟，实际上碰不到这个限制，但仍然要做截断检测。

目标：

- 切分点由 VAD 决定，尽量落在停顿处；
- 只在必须硬切时才使用重叠窗口；
- 有词级时间戳时，按时间戳加 LCS 合并重叠区域；
- 请求超时时自动把窗口拆小重试；
- 自动发现高风险边界，只截取局部音频重新识别；
- 识别、VAD、对齐等后端都可以替换，参数全部可配置。

**本次不做**：评测集与评测脚本、Forced Alignment 的具体实现（只预留接口）、Qwen3-ASR、Google 原生 API。

## 先验证（spike，需要 `OPENROUTER_API_KEY`）

文档没写清的几件事会直接决定设计走哪条分支，所以实现前先用真实请求验证：

1. `google/gemini-3.5-transcribe` 是否接受 `verbose_json`，是否返回 `words`，字段格式和单位是什么。中日韩文字是按字还是按词给时间戳。
2. **处理耗时和片段长度的关系**：分别送 30 秒、1 分钟、2 分钟、4 分钟、8 分钟的片段，记录耗时和超时情况，用来确定默认窗口长度。
3. 是否返回 `language`、`confidence`，`prompt` 参数是否生效。
4. 每分钟音频的费用（看 `usage.cost`）。
5. 超时和超长时的错误码、错误信息分别是什么样。

验证脚本放在 `scripts/spike_openrouter.py`，结果写回本文档的“验证结果”一节，然后再确定下面标着“待定”的参数。

## 两种识别模式

验证结果决定后端走哪种模式。两种都要实现，因为别的后端也可能只返回文本。

| | **词级模式**（首选） | **纯文本模式**（兜底） |
| --- | --- | --- |
| 前提 | 后端返回 `words` | 只返回文本，或只有句子级时间戳 |
| 窗口 | 目标 300 秒，硬上限 480 秒，在 VAD 停顿处切 | 把相邻的 VAD 语音区间合并成 ≤ 25 秒的小组，每组一个窗口 |
| 时间戳 | 直接使用词的时间 | 用窗口内各语音区间的时间，按字符数比例分配，精度较低 |
| 合并 | 时间约束下的词级 LCS | 窗口都切在静音处，通常不需要合并；只有硬切时才用字符级 LCS 去重 |
| 对齐插件的价值 | 低 | 高（以后接入 Forced Alignment 主要是为了这个模式） |

模式不靠配置猜。每次运行前，用该音频里第一段约 10 秒的语音做一次**能力探测**请求，结果按 `(base_url, model)` 缓存到 `~/.cache/yakumi/capabilities.json`，以后同一个后端不再探测。也可以在配置里强制指定模式。

## 数据模型

所有时间都是**相对原音频的绝对秒数**，各窗口内的局部时间在进入合并之前统一转换。

| 类型 | 含义 | 位置 |
| --- | --- | --- |
| `AudioWindow` | 一次识别请求覆盖的音频区间：`index`、`start/end`（实际送去识别的范围，含 padding 或重叠）、`core_start/core_end`（该窗口“负责”的区间，所有窗口的 core 无缝拼接、互不重叠）、`left/right` 边界类型 | `asr/types.py` |
| `Boundary` | 相邻窗口之间的切点：`time`、`kind`（`silence` 或 `forced`）、`overlap`、`gap` | 同上 |
| `Word` | 一个词：`start`、`end`、`text`、`window`，可选 `confidence` | 同上 |
| `TranscriptPiece` | 一个窗口的原始识别结果：`window`、`words`、`text`、`language`、`cost`、`elapsed`、`truncated` | 同上 |
| `Segment` | 最终字幕分段，沿用现有定义 | `models.py` |

## 后端能力声明

```python
@dataclass
class ASRCapabilities:
    word_timestamps: bool
    segment_timestamps: bool
    max_window: float        # 硬上限（秒），由文件大小上限换算
    target_window: float     # 默认目标窗口长度，词级模式使用
    timeout: float           # 单次请求超时（OpenRouter 为 60 秒）

class ASRProvider(Protocol):
    name: str
    def capabilities(self) -> ASRCapabilities: ...   # 配置值加上探测结果
    def transcribe(self, clip: Path, language: str | None, window: AudioWindow) -> RawTranscript: ...
```

| 后端 | 说明 |
| --- | --- |
| `openai`（默认，指向 OpenRouter） | 扩展现有的 `asr/openai_compat.py`：请求 `verbose_json`，带上 `timestamp_granularities[]=word` 和 `segment`；解析 `words`、`segments`、`language`、`confidence`、`usage.cost`；`verbose_json` 返回 400 时降级为纯文本模式。可选透传 OpenRouter 的 `provider` 选项。 |
| `whisper_cpp` | 本地运行，没有超时和大小限制，整段处理，不切分。 |
| `fake` | 按绝对时间生成固定的词，可以配置成词级或纯文本模式，并能模拟超时，用来测试。 |

不新增 Google 原生 API 后端。

## 模块设计

### 1. VAD（`chunking/vad.py`）

```python
class VAD(Protocol):
    def speech_regions(self, audio: Path) -> list[tuple[float, float]]: ...
```

- **`SileroVAD`（首选）**：用 onnxruntime 运行 Silero v5 的 ONNX 模型（约 2MB，MIT 许可）。ffmpeg 把音频解码成 16kHz PCM 流，numpy 每 512 个采样一帧推理，再用迟滞阈值（开 0.5 / 关 0.35）合并成语音区间。
  - 模型首次使用时下载到 `~/.cache/yakumi/models/`，固定版本和 sha256，校验通过才使用。
  - 安装方式：Termux 用 `pkg install python-onnxruntime`，其他平台用 `pip install 'yakumi[vad]'`。
- **`EnergyVAD`（兜底）**：用 ffmpeg `silencedetect` 求出语音区间。不需要额外依赖，但遇到背景音乐会失效。onnxruntime 不可用时自动降级，并给出提示。
- **`NoVAD`**：不检测，全部按硬切处理。
- **VAD 只用来选切点，不删除任何音频**。窗口始终覆盖连续的原始音频，短促语音和感叹词不会因为 VAD 漏检而丢失。纯文本模式下，长段静音也包含在相邻窗口里，只是不单独发请求。

### 2. 切分规划（`chunking/planner.py`）

`plan_windows(duration, speech_regions, caps, cfg) -> (windows, boundaries)`，是纯函数，方便测试。

**词级模式**：

1. 总时长不超过 `target × (1 + tolerance)` 时，整段作为一个窗口。
2. 候选切点取所有长度不小于 `min_gap`（默认 0.3 秒）的静音区间的中点。
3. 贪心推进：在 `[cursor + target×(1−tol), min(cursor + target×(1+tol), cursor + max_window − 2×overlap)]` 范围内，选“静音最长，长度相近时离 target 最近”的候选点，作为 `silence` 边界。padding（默认 0.4 秒）限制在静音区间之内。
4. 范围内找不到静音时，作为 `forced` 边界：切在 target 附近语音最弱的帧上，两侧各加 `overlap`（默认 1.5 秒）重叠。

**纯文本模式**：从头依次累加 VAD 语音区间，加上下一个区间后会超过 `text_window`（默认 25 秒）时，就在两个区间之间的静音处切开。单个语音区间本身超过上限时，按词级模式的第 4 步硬切。

两种模式在输出时都要校验：每个窗口不超过 `max_window`；所有窗口的 core 首尾相接，完整覆盖整段时长。

### 3. 分窗识别与超时自适应（pipeline 的 `transcribe` 阶段）

- 用 ffmpeg 从主音频切出窗口片段，编码为 16kHz 单声道 **16 位** FLAC（无损，约 0.8MB/分钟，远低于 25MB）。
- 每个窗口的结果缓存为 `pieces/window_###.json`。中断后续跑时，已经完成的窗口直接跳过。
- **超时自适应**：窗口请求超时（408、504、连接超时）或被判定为截断时，在窗口中点附近最好的 VAD 静音处把它一分为二，分别重试，可以递归，最短 15 秒。同时把后续窗口的 target 调整为超时窗口长度的 0.6 倍，这些调整记录在 state 里。
- 没有指定原语言、而后端返回了语言时，用第一个窗口识别出的语言**锁定**后续窗口。
- 每个窗口的 `usage.cost` 和耗时累加，记入 state。
- 截断检测：VAD 显示片段末尾 2 秒内仍有语音，但最后一个词的结束时间比片段末尾早超过 `truncation_slack`（默认 3 秒），就判定为截断。纯文本模式改用文本长度和语音时长的比例粗略判断。

### 4. 合并（`chunking/merge.py`）

1. 把局部时间换算成全局时间：`word.start += window.start`。同时清理时间戳：`end < start` 时交换或截断，时长为 0 的词向后扩展到下一个词的开始，最多 0.1 秒。这类不超过 0.1 秒的小异常不算风险信号。
2. **`silence` 边界**：按词的中点落在哪个窗口的 core 内来决定归属。
3. **`forced` 边界（有重叠）**：
   - 取 A、B 两个窗口在重叠区 `[b − overlap, b + overlap]` 内的词，统一大小写、去掉标点，中日韩文字按字拆开；
   - 在时间约束下求 LCS：两个词只有在 `|tA − tB| ≤ match_tolerance`（默认 0.5 秒）时才算匹配；
   - 拼接点选在离切点 b 最近的匹配对上：A 保留拼接点之前的词，B 保留拼接点及之后的词；
   - 没有任何匹配时，退回按中点归属，并把这个边界标记为高风险。
   - 纯文本模式：仅对音频确实重叠的相邻窗口比较 A 的后缀和 B 的前缀。匹配跨越完整文字边界，长度符合实际重叠范围时才去重。不确定的边界保留原文并记录风险。
4. **保留真实重复**：只在相邻窗口的重叠区内去重，同一窗口内的重复永远不动。
5. 输出全局词列表（`words.json`），以及每个边界的诊断信息。

### 5. 生成字幕分段（`subtitle/segment.py`）

- 词级模式：在句末标点、大于 0.6 秒的停顿、字数或时长超过上限这几种位置断开，时间取首词和末词的时间。
- 纯文本模式：把窗口文本按标点切成句子，再按字符数比例分配到窗口内的各个 VAD 语音区间上。

两种模式的结果最后都交给现有的 `align()` 做清理和合并碎片。

### 6. 边界修复（`chunking/repair.py`）

**风险检测**（逐个边界，以及被截断的窗口）：

| 信号 | 判定方法 |
| --- | --- |
| 疑似漏字 | 边界 ±`context` 内，VAD 判定有语音的区间里连续超过 0.8 秒没有任何词（仅词级模式） |
| 疑似重复 | 边界两侧出现相同的规范化 3-gram，且来自不同窗口 |
| 合并失败 | `forced` 边界的 LCS 没有找到任何匹配 |
| 时间异常 | 词的时间倒序；时长为 0 或大于 3 秒；越出所属窗口；相邻词重叠超过 0.2 秒 |
| 截断 | 窗口被标记为 `truncated`（超时拆分仍然失败时） |
| 置信度低 | 后端返回了 `confidence` 时，边界附近的词平均置信度低于阈值 |

**修复流程**：

1. 截取连续的原始音频 `[b − context, b + context]`，`context` 默认每侧 6 秒，可配置。
2. 重新识别这段音频，结果换算成全局时间。
3. 只替换内圈 `[b − context + 1, b + context − 1]` 范围内的内容，两端与原有序列再做一次 LCS 拼接。
4. **采纳规则**：修复结果必须消除了检测到的问题，而且没有引入新的异常；有置信度时要求不低于原结果。不满足就保留原结果，并记一条警告。
5. **预算**：每个任务最多修复 `max_repairs` 处（默认 20）。额外的请求数、耗时和费用写进 `repair.json` 和 state。

### 7. 对齐接口（`align/forced.py`）

只定义 `Aligner` 协议：`align(audio, words) -> words`。默认实现 `NoopAligner` 原样返回。以后可以加基于 torch 的插件，主要用来提升纯文本模式的时间精度；对齐只用来校准时间戳，不修改文本。

## 流水线变化

```
extract → vad → probe → transcribe(分窗) → merge → repair → segment → translate → write → burn
```

- `extract`：主音频改为 `audio.wav`（16kHz 单声道 16 位），作为 VAD、切片和复核的统一来源。原计划用 FLAC，但实测从 mp3 转出的 FLAC 默认是 24 位，体积是 WAV 的两倍，在手机上解码一遍要 2 分多钟；WAV 不需要解码，切片只要零点几秒。
- 新增的产物：`vad.json`、`windows.json`、`pieces/`、`words.json`、`repair.json`。
- `STAGES` 和 `WEIGHTS` 相应更新。旧任务的 state 缺少新阶段时，从 `vad` 开始重新执行。
- 删除 `media.split_audio`，不再使用 `MAX_UPLOAD_BYTES` 和 `CHUNK_SECONDS`。

## 配置

```toml
[asr]
provider = "openai"                         # OpenAI 兼容接口
base_url = "https://openrouter.ai/api/v1"
model = "google/gemini-3.5-transcribe"
# api_key = "..."                            # 或者设置 OPENROUTER_API_KEY
mode = "auto"                               # auto（探测）| words | text
timeout = 60                                # 服务端单次请求超时
target_window = 300                         # 词级模式的目标窗口（秒），属于后端能力
max_window = 480                            # 硬上限，由 60 秒超时决定
upload_format = "flac"                      # flac（16 位无损，约 0.8MB/分钟）| opus（32kbps，约 0.21MB/分钟）

[translate]
base_url = "https://openrouter.ai/api/v1"   # 默认也走 OpenRouter，可以共用一个密钥
model = "..."                               # 由你选择

[chunking]
vad = "silero"                              # silero | energy | none（没有 onnxruntime 时自动改用 energy）
vad_threshold = 0.5
text_window = 25                            # 纯文本模式的窗口上限
min_window = 15                             # 超时拆分时的最短窗口
tolerance = 0.3
min_gap = 0.3
padding = 0.4
overlap = 1.5
match_tolerance = 0.5
truncation_slack = 3.0

[workspace]
keep_audio = false                          # 任务完成后删除 audio.wav（约 115MB/小时），需要时自动重新抽取

[repair]
enabled = true
context = 6.0
max_repairs = 20
```

密钥读取顺序：`YAKUMI_ASR_API_KEY` → `OPENROUTER_API_KEY` → `OPENAI_API_KEY`；翻译同理。

## 文件清单

- **新增**：`asr/types.py`、`chunking/__init__.py`、`chunking/vad.py`、`chunking/planner.py`、`chunking/merge.py`、`chunking/repair.py`、`chunking/probe.py`、`subtitle/segment.py`、`align/forced.py`、`scripts/spike_openrouter.py`
- **修改**：`asr/base.py`、`asr/openai_compat.py`（`verbose_json`、词、费用、400 降级、超时识别）、`asr/whisper_cpp.py`、`asr/fake.py`、`asr/__init__.py`；`pipeline.py`（新阶段、分窗缓存、超时自适应）；`media.py`（`extract_clip`、FLAC、PCM 流，删除 `split_audio`）；`config.py`；`cli.py`（`doctor` 检查 onnxruntime、Silero 模型、OpenRouter 密钥，`--online` 时跑能力探测）；`pyproject.toml`（`vad` 可选依赖，numpy）；`yakumi.example.toml`；`README.md`
- **测试**：`test_planner.py`、`test_merge.py`、`test_repair.py`、`test_vad.py`、`test_probe.py`；更新 `test_providers.py`、`test_pipeline.py`

## 实施顺序

1. ✅ Spike（需要 `OPENROUTER_API_KEY`）：确定是哪种模式，以及默认窗口长度。
2. ✅ 数据类型、能力声明，扩展 `openai_compat`，迁移其他后端，保持现有测试全部通过。
3. ✅ VAD（Silero v6.2.3 ONNX，约 48 倍实时速度；energy 兜底）和切分规划器（两种模式，含超时拆分）。
4. ✅ 能力探测；分窗识别：窗口缓存、超时自适应、语言锁定、截断检测、费用统计。合并暂时只按词的中点归属，LCS 在第 5 步加入。
5. ✅ 合并（时间约束 LCS 拼接、保留真实重复、一侧漏词时取另一侧、纯文本模式去重），以及生成字幕分段。
6. ✅ 边界修复（漏字、重复、合并失败、时间异常、截断五类信号；局部重新识别后用 LCS 拼回；只按结构性指标决定是否采用）。
7. ✅ 对齐接口（`yakumi.align`，默认不对齐，结果必须与输入一一对应且文本不变）、配置、`doctor`（VAD、识别能力缓存、OpenRouter 密钥在线校验）、README；网页任务页显示识别统计与费用。

第 2、3 步不需要密钥，可以和 spike 并行进行。

## 测试与验证

单元测试不依赖网络和密钥：

- **planner**：两种模式下切点都落在静音里；窗口不超过上限；没有静音时变成 `forced` 边界并带重叠；短音频只有一个窗口；所有 core 完整覆盖整段时长。
- **超时自适应**：假后端对超过 N 秒的窗口模拟超时，验证窗口被拆到不超过 N 秒，后续窗口的 target 随之下调，最终结果完整、没有重复。
- **merge**：重叠区里的重复词只保留一份；“no no no”和不同时间说的同一句话被保留；LCS 找不到匹配时退回按中点归属，并标记高风险；跨切点的单词（包括日语音节）不丢失。
- **repair**：分别构造漏字空洞、跨边界重复、截断、时间异常，验证能检测出来；验证拼接替换正确；修复结果更差时保留原结果。
- **openai_compat**：用 MockTransport 验证 `verbose_json` 和 `words` 的解析、`usage.cost` 的统计、400 时降级为纯文本、超时识别。
- **vad**：用 ffmpeg 生成“正弦波 + 静音”交替的测试音频，验证 EnergyVAD 的输出；没有安装 onnxruntime 时跳过 Silero 的测试。
- **pipeline**：用假后端分别在词级和纯文本两种模式下跑完整流程：最终文本与假后端设定的“标准答案”一致；时间戳与原音频对齐；中断后续跑会跳过已完成的窗口。

手动验证（需要 `OPENROUTER_API_KEY`）：

- 用一段约 40 分钟、有连续讲话也有背景音乐的视频跑 `yakumi run`，检查 `windows.json`、`repair.json` 里的切点、超时拆分和修复记录，以及总费用，并抽查边界附近的字幕。
- 用一段带短促感叹词的日语视频，确认感叹词保留、跨切点的音节完整。

## 验证结果（2026-11-21）

测试素材：LibriVox 公有领域有声书，英语和日语（夏目漱石《草枕》）各一段。请求参数为 `google/gemini-3.5-transcribe`、16kHz 单声道 FLAC、`verbose_json`、`timestamp_granularities[]=word,segment`，并指定 `language`。脚本：`scripts/spike_openrouter.py`。

**1. 词级时间戳：可以用，走词级模式。**
- 返回 `text`、`words`、`segments`、`task`、`usage`；`words` 每项为 `{"word", "start", "end"}`，单位是秒，**精度 0.1 秒**。
- `segments` 只有一条，覆盖整个片段，没有实际用处。
- **不返回** `language`、`duration`、`confidence`。因此没法锁定语言，也不能用置信度判断风险。
- 日语按词素切分，标点附在前一个词上（如 `おい、`）。不同请求的切分粒度不一样（同一段音频，有时返回 `が` `ある。`，有时返回 `がある。`），所以**中日韩文字必须按字做 LCS**，不能按词。
- 有时间戳异常：`end < start`（`と` 1.5→1.4），以及时长为 0 的词。合并之前需要先清理。

**2. 处理耗时：大约 5 秒的基础开销，加上每分钟音频 4 秒左右。**

| 片段长度 | 英语耗时 | 日语耗时 |
| --- | --- | --- |
| 30 秒 | 7.2 秒 | 6.5 秒 |
| 1 分钟 | 8.9 秒 | — |
| 2 分钟 | 9.8 秒 | — |
| 4 分钟 | 17.4 秒 | 20.3 秒 |
| 8 分钟 | 25.5 秒 | — |
| 10 分钟 | — | 42.9 秒 |

所有请求都没有超时，但 10 分钟的日语片段已经接近 60 秒上限。每种长度只测了一次，波动范围未知。

**3. 文件大小**：spike 用的是 24 位 FLAC（约 1.9MB/分钟，25MB 大约对应 13 分钟）；改为 16 位后约 0.8MB/分钟，25MB 大约对应 30 分钟。超出时返回 400，错误信息明确（`Audio file exceeds the 25 MB multipart upload limit`）。超时比大小限制先碰到。

**4. 费用**：输入约 25 token/秒，总计约 **$0.0054/分钟**，即约 $0.33/小时。复核的额外费用可以忽略。

**5. 硬切的实际影响**（在 t=120 秒处硬切成两段，和不切的连续识别对比）：
- 英语：没有丢词，切点上的 `other` 被 B 段完整识别。
- 日语：**切点上的「見た」（119.9～120.0 秒）丢了**。A 段停在「を」，B 段从「こと」开始，这正是这套方案要解决的问题。总词数：连续识别 773 个，切开后 722 个（主要是切分粒度不同造成的）。

**6. 结果不确定**：同一段音频，两次请求的结果不完全相同（如「法昌」和「宝生」、「ほうき」和「火鉢」、「いっ」和「なっ」）。这带来两个影响：
- 重叠区里两个窗口对同一段音频的识别结果可能不一样。LCS 必须容忍不一致，按字匹配并加时间约束，拼接点取在匹配上的位置；
- 修复时不能因为“复核结果和原结果不同”就认定原结果有错。采纳规则只看结构性指标：空洞被填上、重复被去掉、时间单调递增。

**据此确定的参数**：

- 目标窗口 300 秒（预计耗时约 25 秒）；
- 容差 ±30%，即最长 390 秒；
- 硬上限 480 秒（预计耗时约 37 秒，为 60 秒超时留出余量，也远低于 25MB）；
- 超时自适应拆分保留，用来应对耗时波动。

### 端到端实测（第 4 步完成后）

17.7 分钟日语有声书，`yakumi transcribe -s ja`：Silero VAD 选出 3 个窗口，3 次请求（能力探测结果已缓存），每次 18～21 秒，总耗时 **1 分 16 秒**，费用 $0.096。改用 WAV 主音频之前，同样的任务要 6 分 25 秒，多出来的时间都花在 24 位 FLAC 的编解码上。

### 强制切点合并实测（第 5 步完成后）

同一段 4 分钟日语，关闭 VAD、每 60 秒强制切一刀（两侧各重叠 1.5 秒），4 次请求，费用 $0.022。
三个切点全部由时间约束 LCS 拼接成功（匹配单元 13～16 个），没有一处标为高风险。
与整段连续识别逐个切点对比（切点前后各 5 秒）：没有重复，也没有丢字；
硬切时丢失的「見た」（120 秒处）这次完整保留（「高砂を見たことがある」）。
剩余差异都是两次识别本身的不确定性（法昌 / 宝生、閑静 / 間歇、狐 / 狼），与切分无关。

拼接点的选择：在两边都落在词尾的匹配对中，取拼接后重叠区内容最多的那个（一侧漏词时自动取另一侧的版本），
内容一样多时取离切点最近的。

### 边界复核实测（第 6 步完成后）

- 正常模式（17.7 分钟日语，Silero 在 333 秒和 710 秒的停顿处切分）：两个边界都没有风险信号，**没有发出任何复核请求**，没有误报。
- 强制硬切（4 分钟，每 60 秒一刀）：60 秒处检测到 1 个时间异常，复核 1 次（12 秒片段，$0.0011），异常消除，结果被采用。
  复核后该处文字从「一生を待つ…間歇」变成「一生増す…閑静」，和整段连续识别的结果一致。
  采纳规则只看结构性指标，不判断文字对错：这次的文字变化是附带的，两个版本都不完全准确（原文是「一升枡」）。

检测阈值：有语音却连续 0.8 秒没有词才算疑似漏字。每个词前后各留 0.25 秒余量，
所以实际能发现的是约 1.3 秒以上的缺失；单个短词（如「見た」）的缺失靠合并阶段的重叠区拼接来解决，复核发现不了。

### 真实素材：76 分钟耳语 ASMR（日语 VTuber 直播）

**第一次运行**（停顿处切分只留 0.4 秒余量，不重叠）：16 个窗口，$0.29，4.5 分钟。但 Silero 在 4554 秒里只判出
188 秒语音，**87% 的识别词落在 VAD 判定的“静音”里**：耳语几乎不被当作语音。8 个“停顿处”切点里有 6 个
其实切在说话中间（切点前后 3 秒内有 4～15 个词）。漏字检测同样依赖 VAD，所以也发现不了这些问题。

**改动**：不再相信 VAD 判定的“静音”一定没人说话。
- 所有切点两侧都重叠 1.5 秒（原来只有强制切点重叠），删除 `padding` 选项；VAD 只用来决定切在哪里。
- 合并时每个切点都走时间约束 LCS 拼接。重叠区里没有词时按中点归属，记为 `empty`，不算风险；
  有词却对不上，才标为高风险。

**同一视频重跑**（切点位置完全相同，因为 VAD 结果一样）：16 个窗口，$0.291（重叠带来的额外费用可以忽略），4 分 47 秒。
- 6 个“停顿处”切点在重叠区里有词，全部由 LCS 拼接（匹配 4～9 个单元）；另外 2 个确实没人说话，记为 `empty`。
- 旧结果中切点处被截断的痕迹消失了：「ちっけっけっ結果」→「実験結果」，「耳の中をねて水観察」→「耳の中をね観察」。
- 两个强制切点仍为高风险：一处是 B 窗口片段开头 0.4 秒内只听到半个音（识别成「くん」），按中点归属后保留了 A 的「最終的に」，结果正确；
  另一处是 B 没听出很轻的「ふわ」，复核后采用。

**片段边缘规则**（`edge_margin = 0.5`）：重叠区里，B 片段开头和 A 片段结尾的 0.5 秒内，另一侧往往只听到半个音，
所以那里对不上不作为风险依据，只看两个片段都可靠的区间 [B 开头 + 0.5, A 结尾 − 0.5]。
只有边缘残音时记为 `edge`。对同一任务只重跑合并和复核：1849.8 秒从高风险变为 `edge`，不再触发复核；
2459.2 秒（「ふわ」离片段边缘 2 秒）仍为高风险，复核后采用。
