# Yakumi

轻量、易用的自动化视频翻译工具：**视频进，翻译字幕出**。

```
视频 → 抽取音频 → VAD 选切点 → 分窗语音识别 → 合并重叠区 → 边界复核 → LLM 断句 → 整句翻译 → SRT / ASS / VTT → （可选）烧录进视频
```

- **命令行**：一条命令完成识别、翻译和导出。
- **网页工作台**：上传视频、查看进度、逐条校对字幕、切换单语或双语、重新生成并下载。
- **长视频可靠**：在停顿处切分；找不到停顿时重叠切分，并按时间对齐去重；高风险的切点会截取局部音频重新识别。
- **按句翻译**：模型按语义断句（不改动任何文字），整句翻译后再按字幕条分段，语序不同的语言之间也通顺。
- **背景资料**：按频道、节目保存一两句背景说明（写进翻译提示词）和可选的固定译法，在多个视频里复用。
- **术语扫描与审核**（可选）：翻译前让模型找出人名、昵称、作品名等，包括被识别写错的写法；人工确认后按位置提示给翻译模型，统一译名，不改动字幕原文。
- **后端可插拔**：识别和翻译都走 OpenAI 兼容接口，默认用 OpenRouter，也可以接本地的 whisper.cpp、Ollama 等。
- **依赖少**：核心只需要 `click`、`httpx`、`flask` 和 ffmpeg；装了 onnxruntime 时启用 Silero VAD。
- **断点续跑**：每个阶段、每个识别窗口的结果都会保存，中断后重新执行同一命令即可从断点继续。

## 安装

需要 Python 3.11 以上，以及带 libass 的 ffmpeg（烧录字幕需要）。

```bash
# Termux
pkg install python ffmpeg python-onnxruntime
# macOS
brew install ffmpeg

cd yakumi
pip install -e .              # 其他平台想用 Silero VAD：pip install -e '.[vad]'
yakumi doctor --online        # 检查运行环境、配置和密钥
```

没有 onnxruntime 也能运行，VAD 会改用 ffmpeg 静音检测，但背景音乐较强时切点可能不准。

两个可选组件，需要时再装：

```bash
pip install -e '.[ytdlp]'     # 网页上的“视频链接”：用 yt-dlp 下载后识别（也可以用系统里的 yt-dlp 程序）
pip install -e '.[server]'    # waitress：部署到服务器时，yakumi serve 自动使用它
```

## 配置

默认通过 [OpenRouter](https://openrouter.ai) 调用 Gemini 3.5 Transcribe 识别、Gemini 3.8 Flash 断句和翻译，只需要一个密钥：

```bash
export OPENROUTER_API_KEY=sk-or-...
```

识别和翻译也可以分别使用 `YAKUMI_ASR_API_KEY`、`YAKUMI_TRANSLATE_API_KEY`。未设置专用密钥时，OpenRouter 和 OpenAI 的官方 HTTPS 地址分别使用 `OPENROUTER_API_KEY` 和 `OPENAI_API_KEY`。切换到其他服务时，可以在指定新地址的同一配置层显式提供该服务的密钥。断句默认继承同一服务的翻译密钥，连接到另一服务时可在 `[segment]` 中单独设置 `api_key`。

也可以写进配置文件：把 `yakumi.example.toml` 复制为 `~/.config/yakumi/config.toml`（全局）或当前目录下的 `yakumi.toml`（项目级），只写需要修改的项。例如换一个翻译模型：

```toml
[translate]
model = "deepseek/deepseek-chat"
```

配置的优先级：内置默认值 → 全局配置 → 项目配置 → 环境变量 → 命令行参数。用 `yakumi -c 文件` 指定配置文件时，只读取这一个文件，代替全局配置和项目配置。配置文件里有拼错、不认识的配置项时会给出提示。

### 翻译模型的思考档位与路由

Gemini 3.8 Flash 的思考不能关闭，档位可选 `low` / `medium` / `high`，默认 `medium`。思考 token 按输出价格计费，档位越高越慢越贵：

| 档位 | 30 条日语字幕（实测） | 说明 |
| --- | --- | --- |
| `low` | 约 4 秒，$0.001 | 最快；短句、语气词偶有误译 |
| `medium` | 14～45 秒，$0.004～0.006 | 默认 |
| `high` | 约 52 秒，$0.03 | 很慢，一般不需要 |

翻译默认 4 批同时请求（`concurrency`），所以档位带来的延迟不会线性累加。

路由模式 `routing`（仅 OpenRouter）：`default` 由 OpenRouter 综合挑选；`floor` 按价格排序并允许半价的 flex 服务档，但不保证一定命中；`nitro` 按吞吐排序并允许更贵的 priority 服务档。更细的控制可以写在 `[translate.provider_routing]` 里，会作为 `provider` 字段原样透传。

```toml
[translate]
reasoning_effort = "low"
routing = "floor"
```

## 命令行

```bash
# 识别并翻译成中文，输出双语 SRT，同时生成烧录字幕的视频
yakumi run talk.mp4 -t zh --bilingual --burn
# → talk.zh.srt  talk.zh.mp4
# 识别：词级时间戳，3 个窗口，3 次请求，边界无需复核，费用 $0.0964

# 指定原语言、字幕格式和输出目录
yakumi run talk.mp4 -t ja -s en --format ass -o out/

# 只识别，不翻译
yakumi transcribe talk.mp4

# 翻译已有字幕
yakumi translate talk.en.srt -t zh

# 使用背景资料，并补充只对这个视频有效的说明
yakumi run stream.mp4 -t zh -x 我的频道 --notes "这期是第一次 ASMR 直播"

# 翻译前扫描术语，暂停等待审核；审核完成后重新执行同一命令，从翻译继续
yakumi run stream.mp4 -t zh -x 我的频道 --terms review
yakumi terms review <任务 ID>

# 把字幕烧录进视频
yakumi burn talk.mp4 talk.zh.srt

# 查看最近的任务
yakumi jobs
```

再次执行 `yakumi run` 时，Yakumi 会比较视频内容、语言和各阶段配置，重跑发生变化的阶段及其下游。只改变 `--bilingual`、`--burn`、`--format` 时，可以继续使用已有的识别和翻译结果。升级后首次遇到没有输入指纹的旧命令行任务时，会建立新任务，旧任务仍可在工作台查看。加上 `--force` 可从头开始。

运行结束时会显示识别统计和费用。费用直接取自 OpenRouter 返回的 `usage.cost`，Gemini 3.5 Transcribe 实测约 $0.33/小时音频。

### 断句与整句翻译

识别结果里的停顿和标点都不等于句子边界：说得慢时一句话中间会停好几秒，口语里也常常缺标点。Yakumi 把**句子**（翻译单位）和**字幕条**（显示单位）分开处理：

- **断句**：默认让翻译用的模型（思考档位 `low`）按语义断句。模型只能插入换行，返回的文字去掉空白后必须和原文一致（允许极少的写法差异），否则这一段改用规则断句。句子的文字和时间都取自识别的词，不会被改动。
- **字幕条**：在每句内部按停顿、字数和时长拆分，所以字幕仍然跟着说话的节奏出现和消失。
- **整句翻译**：整句交给模型翻译，再按原文的分段切开译文，每段对应同一时段。

```toml
[segment]
mode = "rules"        # 不调用模型，只按句末标点断句

[translate]
unit = "cue"          # 逐条翻译（旧的方式）
```

### 背景资料与补充说明

翻译提示词会告诉模型：原文是**不可靠的语音识别结果**，可能有同音误写、漏字、断错的句子，人名和专有名词尤其容易写错，要结合上下文按说话人的本意翻译。所以多数情况下，只要让模型知道**这是谁的什么内容**就够了，它自己能认出被写错的名字并统一译法。这类背景按频道、节目保存成**背景资料**，在多个视频里复用。每份背景资料包含：

- **背景说明**（核心）：一两句话说明主播、节目、粉丝名等，原样写进翻译提示词。例如：“hololive 秘密结社 holoX 成员博衣こより（博衣小夜璃）的 ASMR 直播，粉丝名为“助手くん”（助手君）。” 实测这一句就让 こよ / ジョシュ君 / じゃしくん 等写法统一译成了“小夜璃”“助手君”；
- **固定译法**（可选）：以实体为单位，包括规范名称、已确认的其他称呼（别名）、类别，以及按目标语言的译名，只在必须固定译法时添加，例如作品名、官方译名。翻译时每一批只附上这一批原文里出现过的实体。

任务还可以填写只对这个视频有效的**补充说明**，例如“这期是第一次 ASMR 直播”，同样写进提示词。

在网页上点击右上角的“背景资料”管理；上传视频时在“背景资料与补充说明”里选用；已完成的任务可以在任务页更换后“重新翻译”（不会重新识别，但会覆盖校对过的译文）。命令行：

```bash
yakumi context notes 我的频道 "VTuber 的直播，粉丝名为“助手くん”（助手君）"   # 不存在时自动创建
yakumi context set 我的频道 原文 译文            # 需要固定译法时再加（译名不限定语言）
yakumi context set 我的频道 白上フブキ 白上吹雪 -l zh -a フブちゃん --category person
yakumi context merge 我的频道 白上フブキ フブキ   # 把几条合并成同一实体：后者成为别名
yakumi context import 我的频道 terms.csv         # 批量导入固定译法：每行“原文,译文”，也支持 Tab 和“原文 = 译文”
yakumi context list
yakumi context show 我的频道                      # 译名相同的条目会提示是否合并
```

背景资料保存在 `~/.config/yakumi/glossaries/<名称>.toml`，也可以直接编辑：

```toml
version = 2
name = "hololive"
notes = "VTuber 直播常用术语"

[[entries]]
id = "shirakami-fubuki"            # 可省略，默认等于 canonical
canonical = "白上フブキ"
category = "person"                 # person / group / fan_name / work / song / game / place / term / other
aliases = ["フブキ", "フブちゃん"]   # 只放确认过、适合长期复用的称呼
translations = { "zh" = "白上吹雪" } # 按目标语言；"*" 表示任何语言，空字符串表示保留原文
```

语言代码 `zh`、`zh-CN`、`zh-Hans` 视为同一种，`zh-TW`、`zh-Hant` 视为同一种。旧格式（`[terms]` 下“原文 = 译文”）照常读取，每条成为一个实体、译名不限定语言，保存时改写为新格式；译名相同的旧条目只会提示合并，不会自动合并。

配置文件里 `[translate]` 的 `notes`（全局背景说明）和 `[translate.glossary]`（全局固定译法，“原文 = 译文”）对所有任务生效。实体按 id 或规范名称合并，优先级为：任务的术语审核结果 ＞ 选用的背景资料（后选的覆盖先选的）＞ 全局配置。旧的 `yakumi glossary` 命令和 `-g` 选项仍然可用。

### 术语扫描与审核

背景说明能解决大多数名字问题。内容涉及很多人物、作品，或者需要逐个确认译名时，可以在翻译前做一次术语扫描：

1. **扫描**：按约 4000 字分块（相邻两块略有重叠），连同视频标题、背景说明和已知的固定译法交给模型（默认 `deepseek/deepseek-v4.1-flash-1017`，可在 `[terms]` 或网页设置里修改），找出人名、昵称、团体、作品、歌曲、游戏、粉丝称呼和社区用语，包括被识别写错的写法。模型不确定时，规范名称和译名留空。
2. **校验与关联**：每个结果都必须是对应句子里逐字存在的片段，找不到时做一次定向修复，仍然不行的记为异常、不使用。同一实体的不同称呼并在一起，优先对应到背景资料里已有的实体；同一称呼在其他句子里的出现位置会自动补全。只由已确认的名称或别名逐字匹配到的实体直接标为通过。
3. **审核**：任务暂停（状态“待审核”），在任务页的“术语审核”里逐个**通过、修改或拒绝**，可以单独排除某一处出现，也可以把几个候选合并。点“完成审核，继续翻译”后，还在待审核的不会使用。
4. **翻译**：通过的术语按句子位置提示给翻译模型，例如“第 3 行的「博士小寄り」指「博衣こより」”，字幕原文不会被改动。每一批只附上与这一批有关的术语。

审核结果只对这个任务有效，保存在任务目录的 `term_review.json`。可以把通过的实体“保存到背景资料”，只会保存规范名称、你确认的别名和这种语言的译名，识别错误的写法不会进入背景资料。

新建任务时在“背景资料与补充说明”里选择术语扫描方式；已完成的任务可以在任务页点“扫描术语”，扫描和审核后重新翻译，不会重新识别。三种方式：`off` 不扫描（默认）、`review` 扫描后人工审核、`auto` 不暂停，只自动采用对应到背景资料已有实体的结果。命令行：

```bash
yakumi run stream.mp4 -t zh --terms review     # 扫描后暂停
yakumi terms show <任务 ID> [--all]             # 列出术语（--all 显示出现位置）
yakumi terms review <任务 ID> [--save-to 我的频道]   # 逐个审核：通过 / 修改后通过 / 拒绝 / 跳过
yakumi terms set <任务 ID> 3 --approve --translation 博衣小夜璃
yakumi terms set <任务 ID> 5 --merge-into 3     # 把第 5 个合并到第 3 个
yakumi terms done <任务 ID>                     # 结束审核
yakumi run stream.mp4 -t zh --terms review     # 再次执行，从翻译继续
```

审核结果改了之后再执行 `yakumi run`，只会重新翻译。

### 字幕标点

中文字幕习惯不用逗号和句号：句中的逗号、句号用空格代替，行尾的直接去掉；问号、感叹号保留。Yakumi 导出时默认按这个惯例处理中文和日文（`[subtitle] punctuation = "subtitle"`），英文等其他语言不变。保存的原文和译文保留完整标点，网页校对时看到的是完整的句子；不需要时设为 `"keep"`。

## 网页工作台

```bash
yakumi serve                    # 默认 http://127.0.0.1:8765，只有本机能访问，不需要登录
```

1. 选择视频和目标语言，点击“开始翻译”。视频可以上传，也可以点“服务器”选择服务器上已有的文件；切换到“视频链接”时粘贴 YouTube、Bilibili 等网站的链接，下载后自动识别（需要 yt-dlp）。“翻译成”选“不翻译”时只识别原文（相当于 `yakumi transcribe`）。
2. 任务在后台依次执行，页面实时显示进度和每个步骤的状态。关闭页面不影响任务。
3. 完成后在“字幕校对”里逐条修改原文和译文，点击时间可以跳到视频对应位置。
4. 选择单语或双语、字幕格式，以及是否烧录，点击“保存并生成”，然后下载。

已经有字幕时，切换到“已有字幕”上传 SRT / VTT：选目标语言就翻译（相当于 `yakumi translate`），选“不翻译”只转换格式；附带视频时可以在线预览，也可以烧录（相当于 `yakumi burn`）。

任务页还可以“从头重新处理”（更换模型或服务后按当前设置重新识别，相当于 `--force`）和删除任务。

右上角的“设置”与命令行共用同一份配置，可以修改识别、翻译、断句、字幕等常用选项和 API 密钥，并显示每一项来自默认值、哪个配置文件还是环境变量：

- 保存到优先级最高的配置文件（`-c` 指定的文件；当前目录有 `yakumi.toml` 时写入它；否则写入全局配置），只改动涉及的行，注释保持不变。新建的文件只有自己可读。
- 密钥只能填写或删除，网页上看不到已保存的值。更换服务地址而没有同时填写新密钥时，会删除原来的密钥，避免发给新地址。
- 由环境变量设置的项在网页上只读；会执行本地程序或读取服务器路径的项（whisper.cpp、字体目录）、任务目录和 `[web]` 只能在配置文件里修改。
- 通过 `YAKUMI_ASR_API_KEY` 或 `YAKUMI_TRANSLATE_API_KEY` 设置密钥时，网页会锁定对应的服务地址。在部署配置中一起修改服务地址与密钥。
- 保存后对之后开始处理的任务生效；“环境检查”和“测试连接”与 `yakumi doctor [--online]` 相同。

网页界面支持浅色、深色和跟随系统，也适配手机屏幕。

### 服务器上的文件

网页上默认只能浏览任务目录（`[workspace] dir`，其中工作台自己的 `jobs/`、`uploads/` 不显示），把视频放进去就能直接选择，不用再上传。需要访问其他目录时加到配置里：

```toml
[web]
file_roots = ["~/Videos", "/mnt/media"]
```

选择的文件原地使用，不会被复制；删除任务也不会删除它。指向允许目录之外的符号链接一律拒绝。

视频链接下载默认限时 3600 秒，下载文件和部分文件的总量上限为 4096 MB。可在 `[download]` 中调整 `max_duration_seconds` 与 `max_size_mb`。工作台让 yt-dlp 忽略其独立配置文件，Cookie 等额外参数可通过 `download.extra_args` 提供。

### 部署到远程服务器

```bash
pip install -e '.[server]'      # 推荐：使用 waitress
yakumi passwd                   # 设置登录密码（保存为哈希）
yakumi serve --host 0.0.0.0     # 对其他设备开放；没有设置密码时会拒绝启动
```

- **登录**：设置密码后，访问工作台需要先登录；可以勾选“保持登录”（`[web] session_days`，默认 30 天）。连续输错 5 次需要等 10 分钟。修改密码后，所有已登录的设备都需要重新登录。容器部署时也可以用环境变量 `YAKUMI_WEB_PASSWORD` 提供密码。
- **HTTPS**：公网访问请放在 nginx、Caddy 等反向代理后面并启用 HTTPS，在配置里写明代理层数和域名。通过 HTTPS 访问时，登录 Cookie 只走 HTTPS。

  ```toml
  [web]
  proxy_count = 1                    # 前面有一层反向代理，信任它转发的 X-Forwarded-For / Proto / Host
  allowed_hosts = ["yakumi.example.com"]
  max_upload_mb = 8192
  ```

  反向代理需要转发 `Host` 或 `X-Forwarded-Host`，上传大小限制也要放宽（nginx：`client_max_body_size 8g;`）。工作台需要部署在域名的根路径。
- **只在可信的局域网里使用**时，可以不设密码：`yakumi serve --host 0.0.0.0 --no-auth`。
- 没有 waitress 时使用 Flask 自带的服务器（会提示安装 waitress）。

视频链接默认拒绝指向本机和局域网地址的链接（`[download] allow_private`），下载格式可以在“设置”里修改（默认最高 1080p）。工作台会拒绝来自其他网站的请求，页面带有 CSP 等安全响应头。详见 [`SECURITY.md`](SECURITY.md)。

## 长视频是怎么处理的

识别接口单次请求有处理时间和文件大小的限制（OpenRouter 上游超时 60 秒），所以长视频必须切开分别识别。直接按固定时长硬切会切断正在说的话，实测会丢字。Yakumi 的做法：

| 阶段 | 说明 |
| --- | --- |
| 抽取音频 | 转为 16kHz 单声道 16 位 WAV，作为切片和复核的统一来源；任务完成后自动删除 |
| VAD | Silero VAD 找出语音区间（约 48 倍实时速度），只用来选切点，**不删除任何音频** |
| 分窗识别 | 每个窗口约 5 分钟，在附近最长的停顿处切开。找不到停顿时强制切分，两侧各重叠 1.5 秒。请求超时时自动在停顿处拆小重试；每个窗口的结果单独缓存 |
| 合并 | 停顿处的边界按时间归属；重叠的边界在时间约束下做 LCS 对齐后拼接：只去掉同一时刻的重复，口头重复和不同时间说的同一句话都会保留；一侧漏词时自动采用另一侧的版本 |
| 边界复核 | 检查每个切点附近有没有漏字、重复、拼接失败、时间异常和截断。有问题就截取前后各 6 秒的连续音频重新识别，结果确实更好才替换 |
| 断句 | 模型按语义把文本切成完整的句子，只允许插入换行；返回的文字与原文不一致时这一段改用规则断句。每句再按停顿（≥ 1.2 秒）、字数和时长拆成若干条字幕，从不跨句 |
| 翻译 | 以句子为单位，每批约 15 句。一句话分成几条字幕时用 `¦` 隔开，要求译文分成同样的段数，每段对应同一时段。段数不对时重试，再不行按字数比例切分整句译文，最后逐条翻译，保证与时间轴一一对应 |
| 生成 / 烧录 | 输出 SRT、ASS 或 VTT（双语时译文在上、原文在下），按字幕惯例处理标点；ffmpeg + libass 烧录 |

识别后端不支持词级时间戳时，会自动降级为纯文本模式：窗口缩短到 25 秒以内，时间轴按语音区间估算。

设计细节和实测数据见 [`docs/design/long-audio.md`](docs/design/long-audio.md)；断句与整句翻译见 [`docs/design/llm-segmentation.md`](docs/design/llm-segmentation.md)。

任务目录（默认 `~/.local/share/yakumi/jobs/<任务 ID>/`，设置了 `$XDG_DATA_HOME` 时在它下面；可用 `[workspace] dir` 或环境变量 `YAKUMI_WORKSPACE` 修改，配置文件里的相对路径相对于该文件所在的目录）。在哪个目录运行 yakumi 都使用同一个任务目录，`yakumi doctor` 会显示实际位置：

```
vad.json  windows.json  pieces/  merged.json  repair.json  words.json
sentences.json  aligned.json  term_candidates.json  term_review.json  translated.json  glossary.json
subtitles.srt  preview.vtt  output.mp4  state.json
```

## 使用本地模型

### 本地识别：whisper.cpp

```bash
git clone https://github.com/ggml-org/whisper.cpp
cd whisper.cpp
cmake -B build && cmake --build build -j --config Release
sh ./models/download-ggml-model.sh base     # 也可以选 small、medium 等
```

```toml
[asr]
provider = "whisper_cpp"
whisper_cpp_bin = "~/whisper.cpp/build/bin/whisper-cli"
whisper_cpp_model = "~/whisper.cpp/models/ggml-base.bin"
threads = 4
```

whisper.cpp 自己处理长音频，Yakumi 不再切分。在手机上建议用 `base` 或 `small` 模型，否则会非常慢。

### 本地翻译：Ollama 等

任何提供 OpenAI 兼容 `/chat/completions` 接口的服务都可以使用：

```toml
[translate]
base_url = "http://127.0.0.1:11434/v1"
model = "qwen2.5:7b"
```

## 开发

```bash
pip install -e '.[dev]'
python -m pytest -q
python -m pyflakes src tests scripts
```

代码结构和扩展点见 [`docs/architecture.md`](docs/architecture.md)，开发约定见 [`CONTRIBUTING.md`](CONTRIBUTING.md)，版本变化见 [`CHANGELOG.md`](CHANGELOG.md)。

测试使用内置的假识别和假翻译，不需要网络和密钥。假识别按绝对时间生成固定的词（`w0 w1 w2 …`），所以测试可以精确验证切分和合并后的结果不多不少。手动体验完整流程时也可以使用它们：

```bash
yakumi run sample.mp4 -t zh --asr fake --translator fake --burn
```

实测 OpenRouter 接口用的脚本：`scripts/spike_openrouter.py`。

## 路线图

- [x] Core：识别、断句、翻译、字幕导出与烧录
- [x] CLI
- [x] 网页工作台（简版 Studio）
- [x] 长音频：VAD 切分、重叠合并、边界复核
- [x] LLM 断句、整句翻译、背景资料
- [ ] 强制对齐后端（接口已预留）
- [ ] Flow：批量任务、监听文件夹、多任务并行
- [ ] AI 配音（TTS）

## 许可

[MIT](LICENSE)
