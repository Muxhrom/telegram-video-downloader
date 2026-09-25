# Telegram 视频下载器

Windows 桌面程序：使用 Telegram 用户账号浏览群聊、超级群组和频道中的历史视频，批量下载并管理队列；可选使用 FFmpeg 无损重封装元数据，再通过本地 OpenList 直连上传到阿里云盘。

## 功能

- Telegram 连接固定使用 SOCKS5 `127.0.0.1:7890`，代理不可用时不会绕过代理直连。
- 服务端媒体筛选、分页加载、名称/日期筛选，支持普通视频、视频文件和圆形视频。
- 已浏览的视频、分页位置和上次聊天会写入本机数据库。重启或切换聊天时立即显示缓存，再在后台检查较新的消息；“重新核对历史”可手动全量检查旧消息。
- 下载管理窗口提供进度、速度、ETA、优先级、暂停、取消和失败重试。
- 大于等于 32 MiB 的视频使用 4 条并行区段请求下载；小文件沿用 Telethon 原下载方式。仍最多同时处理 2 个文件，实际速度取决于 Telegram、代理与磁盘。
- 左侧会话列表显示完整换行名称，侧栏可拖动调宽并记住宽度。
- 下载与上传任务在重启后恢复。传输从零重新开始时，进度也从零显示，不伪装成断点续传。
- 每个群聊可独立开启新增视频自动下载；历史记录按群聊 ID 和消息 ID 去重。
- 下载目录与云盘清单会分别核对。程序区分曾下载、本地存在、云端确认、疑似同名和云端未核实；普通下载会跳过已有的确定副本，用户可手动重新下载。
- 上传管理窗口提供 FFmpeg 处理、WebDAV 上传、远端校验和重试。
- 云盘启用时，启动后会核对云端清单与历史下载记录：本地文件仍在而云端缺失的任务会重新经 FFmpeg 处理并上传；疑似同名文件等待逐条确认。云盘清单提供旧文件改名预览，必须先在当前挂载上实测小文件改名。
- 下载管理支持 H.265 压缩：高质量 CRF 20、平衡 CRF 24、强压缩 CRF 28；支持手动压缩和下载完成后自动压缩。压缩成功并校验后删除本地原视频，下载记录仍按群聊 ID + 消息 ID 保持已下载状态。
- 主界面提供“选中已下载”“压缩全部已下载”“上传全部已下载”和“刷新本地状态”，重启后可直接按本地下载记录批量处理，不必逐条翻页勾选。
- OpenList/阿里云盘通信直接联网，不使用 Telegram 的 Clash 代理。
- 窗口关闭后留在系统托盘，选择“彻底退出”才停止后台任务。

## 快速开始

要求：

- Windows 10/11 x64
- 64 位 Python 3.11、3.12 或 3.13
- PowerShell 7
- Clash 或兼容 SOCKS5 代理，监听 `127.0.0.1:7890`

克隆仓库后执行：

```powershell
Set-Location -LiteralPath '<项目目录>'
& '.\scripts\setup.ps1'
& '.\scripts\run.ps1'
```

`setup.ps1` 会自动寻找受支持的 Python、创建 `.venv` 并安装运行依赖。以后启动只需执行 `scripts\run.ps1`。

开发环境与测试：

```powershell
& '.\scripts\setup.ps1' -Development
& '.\scripts\test.ps1'
```

完整开发接管说明见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)，模块与数据流见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 首次登录 Telegram

1. 启动 Clash，确认 SOCKS5/混合端口为 `127.0.0.1:7890`。
2. 登录 [my.telegram.org](https://my.telegram.org)，在 **API development tools** 创建应用并取得自己的 `api_id` 和 `api_hash`。
3. 启动程序，填写 API 信息和手机号，再输入 Telegram 客户端收到的验证码。
4. 如果账号启用了二步验证，继续输入二步验证密码。

程序只读取消息和下载媒体，不会发送、转发或删除 Telegram 消息。

## 配置阿里云盘上传

1. 打开“云盘设置”，安装并启动程序管理的本地 OpenList。
2. 点击“打开管理页”，使用窗口显示的管理员账号和密码登录。
3. 在 OpenList 的“存储”页面添加“阿里云盘 Open/OAuth2”，挂载路径填写 `aliyun-drive`。
4. 返回程序点击“测试挂载”。
5. 选择现有 `ffmpeg.exe`，或使用程序提供的自动安装功能。

上传前 FFmpeg 使用 `-c copy` 保留音视频流，只执行容器与元数据标准化。此功能用于文件整理和兼容性，不用于规避云盘审核。OpenList、FFmpeg 下载和云盘上传默认直接联网；仅 Telegram 使用 `127.0.0.1:7890`。

新上传的云端文件名包含聊天 ID 与消息 ID，便于在本地文件删除后继续准确识别。旧云端文件若有可信上传记录，可在“云盘清单”中预览并手动改名；仅同名的文件需要逐条确认。网盘自动补传不会删除本地文件。

压缩使用 FFmpeg `libx265` 软件编码，音频和字幕尽量直接复制。输出经 FFprobe 校验，若失败或压缩后没有变小则保留原文件。上传尚未完成时，程序会暂存原视频供云端上传，本地继续保留压缩后的版本。

## 本地数据与隐私

用户数据不保存在仓库或 EXE 旁边：

- 设置、数据库、Telegram 会话和日志：`%LOCALAPPDATA%\TelegramVideoDownloader`
- 默认视频目录：`%USERPROFILE%\Downloads\Telegram Video Downloader`
- API Hash 与 OpenList 管理员密码：Windows 凭据管理器
- 上传临时文件：`%LOCALAPPDATA%\TelegramVideoDownloader\upload_staging`
- 压缩临时文件：`%LOCALAPPDATA%\TelegramVideoDownloader\compression_staging`

不要提交或分享 `telegram.session`、`state.sqlite3`、`config.json`、日志、OpenList `data` 目录或下载的视频。更完整的边界说明见 [PRIVACY.md](PRIVACY.md)。

现有单文件 EXE 可以继续运行。新版本部署前，请先在旧程序中选择“彻底退出”，确认没有进行中的下载或上传，再复制新 EXE 到原位置；程序会沿用上述用户数据目录。不要在旧程序运行时同时启动新版本并访问同一份数据库或 OpenList。

提交前可以运行：

```powershell
& '.\scripts\check-privacy.ps1'
```

## 构建免安装 EXE

```powershell
& '.\build.ps1'
```

脚本会准备开发依赖、运行全部测试并使用 PyInstaller 构建 `dist\telegram视频下载器.exe`。通常耗时 3–10 分钟，首次下载依赖时可能更久。生成的 EXE 和构建缓存均被 Git 忽略。

## 故障排查

- **代理未启动**：确认 Clash 的 SOCKS5/混合端口为 7890，然后点“测试代理”或“登录 / 重连”。
- **收不到验证码**：验证码通常发送到已登录的 Telegram 客户端，不一定是短信。
- **Telegram 限流**：按界面提示等待，不要反复高频扫描大型群聊。
- **OpenList 无法启动**：查看 `%LOCALAPPDATA%\TelegramVideoDownloader\tools\openlist\openlist.log`。
- **程序运行异常**：查看 `%LOCALAPPDATA%\TelegramVideoDownloader\app.log`。
