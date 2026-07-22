# 架构说明

## 进程与线程

程序只有一个桌面进程，Qt 主线程负责界面。两个后台工作线程各自维护独立的 asyncio 事件循环：

- `TelegramWorker`：登录、群聊列表、媒体扫描、下载队列和新增消息监听。
- `UploadWorker`：FFmpeg 标准化、OpenList 生命周期、WebDAV 查重/上传/校验。

界面通过 Qt Signal 与工作线程交换普通字典和标量。Telegram 的 64 位聊天 ID 使用 `Signal(object)` 传递，避免 Qt 32 位整数截断。

## 核心模块

- `main.py`：创建数据目录、日志、Qt 应用和主窗口。
- `paths.py`：集中定义用户数据、下载目录和工具目录。
- `config.py`：非秘密设置的 JSON 读写。
- `credentials.py`：通过 Windows keyring 保存 API Hash 和 OpenList 密码。
- `storage.py`：SQLite 表结构与下载/上传/自动规则记录。
- `telegram_service.py`：Telethon 登录、筛选分页、下载和自动监听。
- `upload_service.py`：FFmpeg、WebDAV 队列和远端校验。
- `openlist_manager.py`：OpenList 安装、配置、启动、停止和端口选择。
- `ui.py`：主界面、登录和下载管理。
- `upload_ui.py`：云盘设置和上传管理。

## Telegram 数据流

1. 启动前检测 `127.0.0.1:7890`。
2. Telethon 只使用 SOCKS5 代理建立连接。
3. 历史扫描分别使用 Video、RoundVideo 与 Document 服务端筛选。
4. 结果以聊天 ID 和消息 ID 去重后逐批发送到界面。
5. 下载任务写入 `.part`，完成后原子改名并写入 SQLite。
6. 开启自动规则后，新消息事件复用同一媒体识别与下载流程。

切换群聊会取消旧扫描。FloodWait 会保留已有结果并按 Telegram 指示等待。

## 上传数据流

1. 下载成功后按设置进入上传队列。
2. FFmpeg 使用 `-c copy` 写入临时 staging 文件。
3. WebDAV 依次执行 PROPFIND、MKCOL 和 PUT。
4. 同名同大小会补记为已上传；同名不同大小追加消息 ID，不覆盖远端文件。
5. PUT 完成后重新查询远端大小，一致后才记录完成并清理 staging。
6. 程序不提供删除云端文件的操作。

OpenList、FFmpeg 工具下载和 WebDAV 客户端均设置 `trust_env=False` 或清除代理环境；Telegram 代理与云盘直连边界不可混用。

## 持久化结构

用户数据根目录是 `%LOCALAPPDATA%\TelegramVideoDownloader`。

SQLite 包含三张表：

- `auto_rules`：群聊、启用状态、目录和启用时间。
- `downloads`：聊天 ID、消息 ID、最终路径、大小和状态。
- `uploads`：源路径、处理后名称、远端路径、大小、ETag、优先级和状态。

本地文件存在状态是派生信息。删除本地视频不会删除已验证的云端上传记录。

## 扩展新功能

- 新媒体类型：修改 `TelegramWorker._video_info` 并增加 `test_media_detection.py`。
- 新下载状态：同步修改工作线程、主表标签和下载管理窗口。
- 新上传阶段：扩展 `UploadWorker._process_job`、SQLite 状态和上传管理界面。
- 新设置项：先扩展 `AppConfig`，再接入设置界面；秘密不得写入 `config.json`。
- 数据库字段变更：增加向后兼容迁移，不能假设用户数据库为空。
