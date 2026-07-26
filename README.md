# Telegram 视频下载器

Windows 桌面程序：使用 Telegram 用户账号浏览群聊、超级群组和频道中的历史视频，批量下载并管理队列；可选使用 FFmpeg 无损重封装元数据，再通过本地 OpenList 直连上传到阿里云盘。

## 功能

- Telegram 连接固定使用 SOCKS5 `127.0.0.1:7890`，代理不可用时不会绕过代理直连。
- 服务端媒体筛选、分页加载、名称/日期筛选，支持普通视频、视频文件和圆形视频。
- 下载管理窗口提供进度、速度、ETA、优先级、暂停、取消和失败重试。
- 每个群聊可独立开启新增视频自动下载；历史记录按群聊 ID 和消息 ID 去重。
- 下载目录同名比对，已存在的视频会在主列表中变灰标注。
- 上传管理窗口提供 FFmpeg 处理、WebDAV 上传、远端校验和重试。
- 下载管理支持 H.265 压缩：高质量 CRF 20、平衡 CRF 24、强压缩 CRF 28；支持手动压缩和下载完成后自动压缩。压缩成功并校验后删除本地原视频，下载记录仍按群聊 ID + 消息 ID 保持已下载状态。
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

压缩使用 FFmpeg `libx265` 软件编码，音频和字幕尽量直接复制。输出经 FFprobe 校验，若失败或压缩后没有变小则保留原文件。上传尚未完成时，程序会暂存原视频供云端上传，本地继续保留压缩后的版本。

## 本地数据与隐私

用户数据不保存在仓库或 EXE 旁边：

- 设置、数据库、Telegram 会话和日志：`%LOCALAPPDATA%\TelegramVideoDownloader`
- 默认视频目录：`%USERPROFILE%\Downloads\Telegram Video Downloader`
- API Hash 与 OpenList 管理员密码：Windows 凭据管理器
- 上传临时文件：`%LOCALAPPDATA%\TelegramVideoDownloader\upload_staging`
- 压缩临时文件：`%LOCALAPPDATA%\TelegramVideoDownloader\compression_staging`

不要提交或分享 `telegram.session`、`state.sqlite3`、`config.json`、日志、OpenList `data` 目录或下载的视频。更完整的边界说明见 [PRIVACY.md](PRIVACY.md)。

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
