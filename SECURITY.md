# 安全说明

## 网页工作台

网页工作台（`yakumi serve`）默认只监听 `127.0.0.1`，不需要登录。部署到远程服务器时：

- **登录密码**：用 `yakumi passwd` 设置（配置里只保存哈希），或用环境变量 `YAKUMI_WEB_PASSWORD`。监听非本机地址或配置了反向代理（`web.proxy_count`）却没有密码时，`yakumi serve` 拒绝启动，除非明确加上 `--no-auth`（此时同一网络里的任何人都能上传、发起会消耗 API 额度的识别和翻译、修改设置，只适合可信的局域网）。
- 登录状态保存在签名的 Cookie 里（`HttpOnly`、`SameSite=Lax`，通过 HTTPS 访问时加 `Secure`），签名密钥保存在 `~/.config/yakumi/web_secret`（只有自己可读）。Cookie 带有密码指纹，修改密码后旧的登录全部失效。同一来源连续输错 5 次密码需要等待 10 分钟（在反向代理后面却没有设置 `web.proxy_count` 时，所有访问者看起来都来自代理，会共用这个限制）。
- **公网访问请使用 HTTPS**：放在 nginx、Caddy 等反向代理后面，并设置 `web.proxy_count`，否则工作台无法识别 HTTPS 和真实的客户端地址。不要在没有反向代理时设置 `proxy_count`，否则客户端可以伪造 `X-Forwarded-*` 头。
- 页面带有 `Content-Security-Policy`（不允许内联脚本、不允许被嵌入其他网页）、`X-Frame-Options`、`X-Content-Type-Options` 和 `Referrer-Policy`。
- **服务器文件**：网页只能浏览任务目录（不含工作台自己的 `jobs/`、`uploads/`）和 `web.file_roots` 列出的目录；路径解析符号链接后再检查，指向这些目录之外的一律拒绝。能登录工作台的人可以读取这些目录里的视频和字幕，请只添加需要的目录。
- **视频链接**：用 yt-dlp 在服务器上下载。提交和执行前都会检查初始链接的解析地址，默认拒绝本机、局域网等非公网地址，可用 `download.allow_private` 更改。下载时限和体积上限由 `download.max_duration_seconds` 和 `download.max_size_mb` 控制。yt-dlp 的程序路径和额外参数只能在配置文件里设置。重定向和后续资源由 yt-dlp 处理，服务器所在网络里有敏感服务时，请用防火墙限制出站访问。
- 为了防止你正在浏览的其他网页利用工作台，所有修改类请求都会检查 `Origin`，拒绝跨站请求（启用登录后，没有 `Origin` 和 `Referer` 的修改请求也会被拒绝）；`Host` 只接受 IP 地址、`localhost` 和配置 `web.allowed_hosts` 里列出的域名，防止 DNS 重绑定。
- 上传大小默认限制为 4096 MB（`web.max_upload_mb`）。
- 网页“设置”可以修改服务地址、模型和密钥并写入配置文件，所以能访问工作台的人也能改这些设置。密钥只写不读：页面和接口都不会返回已保存的密钥。更换服务地址而没有同时提供新密钥时，会删除这一段原来的密钥，避免把它发给新地址。
- 会执行本地程序或读取服务器路径的配置（`asr.whisper_cpp_bin`、`asr.whisper_cpp_model`、`subtitle.fonts_dir`、`download.ytdlp`、`download.extra_args`）、任务目录和 `[web]`（含登录密码）不能在网页上修改；由环境变量设置的项在网页上只读。
- 原件预览使用探测确认的音视频类型。无法识别为支持的媒体容器时，原件以通用二进制附件返回。
- 媒体探测、抽音和烧录使用本地文件协议与单文件音视频容器白名单。远程部署可用独立服务账号或容器限制媒体处理进程的文件访问范围。

## 密钥

- API 密钥可以放在环境变量（`OPENROUTER_API_KEY` 等）或配置文件里。全局配置 `~/.config/yakumi/config.toml` 建议设置为只有自己可读（`chmod 600`）。
- 项目级配置 `yakumi.toml` 已在 `.gitignore` 中排除，请不要把密钥提交到仓库。
- 任务目录（`.yakumi/`）里不保存密钥，但包含视频、识别文本和译文，分享前请留意内容。

## 外部服务

识别和翻译会把音频片段与文本发送给你配置的服务（默认是 OpenRouter）。处理敏感内容时，请确认服务商的数据政策，或改用本地后端（whisper.cpp、Ollama 等）。

## 报告问题

发现安全问题时，请不要公开提交 issue，先私下联系维护者，说明复现步骤和影响范围。
