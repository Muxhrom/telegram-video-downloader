# 本地数据与隐私边界

本程序不包含遥测、分析 SDK 或远程控制功能。Telegram 消息和视频只在用户授权的 Telegram 会话、用户选择的本地目录以及用户自行配置的阿里云盘之间流转。

## 本地保存内容

`%LOCALAPPDATA%\TelegramVideoDownloader` 保存：

- `config.json`：API ID、手机号、代理和云盘普通设置
- `telegram.session`：Telegram 登录会话
- `state.sqlite3`：下载、上传和自动规则记录
- `app.log`：运行与错误日志
- `tools\openlist\data`：OpenList 配置、数据库和阿里云盘授权数据
- `upload_staging`：上传前的临时处理文件

API Hash 与 OpenList 管理员密码由 Windows 凭据管理器保存。视频默认位于 `%USERPROFILE%\Downloads\Telegram Video Downloader`，也可以保存到用户选择的其他目录。

## 不进入仓库的内容

`.gitignore` 与 `scripts\check-privacy.ps1` 排除会话、日志、SQLite、工具、临时目录、视频、EXE 和 ZIP。测试数据必须保持虚构。

提交问题报告时不要直接上传完整日志。先删除手机号、聊天名称、聊天 ID、消息 ID、本地路径、云盘路径、令牌和文件名；只保留复现问题所需的最小错误片段。

## 网络边界

- Telegram：只通过配置的 SOCKS5 `127.0.0.1:7890`。
- GitHub/OpenList/FFmpeg 下载：优先直接连接，失败后工具安装流程可尝试 Telegram 代理。
- OpenList 管理页与 WebDAV：仅访问 `127.0.0.1`。
- 阿里云盘上传：由本地 OpenList 直接连接，不使用 Telegram 代理。

程序不会发送、转发或删除 Telegram 消息，也不会主动删除本地或云端视频。
