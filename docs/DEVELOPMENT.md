# 开发接管指南

这份文档面向未来重新接手项目的开发者。仓库不依赖原开发电脑中的账号、会话、下载记录或工具二进制。

## 1. 环境准备

支持 Windows 10/11 x64 与 Python 3.11–3.13。推荐安装 PowerShell 7 和 Git。

```powershell
git clone '<仓库地址>'
Set-Location -LiteralPath '.\telegram-video-downloader'
& '.\scripts\setup.ps1' -Development
```

脚本创建项目内的 `.venv` 并安装 `requirements-dev.txt`。所有命令均可从仓库根目录零参数运行：

```powershell
& '.\scripts\run.ps1'
& '.\scripts\test.ps1'
& '.\scripts\check-privacy.ps1'
& '.\build.ps1'
```

不要把旧电脑的 `%LOCALAPPDATA%\TelegramVideoDownloader` 复制进仓库。确实需要迁移账号时，应通过可信的离线方式单独处理，并优先在新电脑重新登录 Telegram、重新授权阿里云盘。

## 2. 依赖文件

- `requirements.txt`：运行程序所需的固定直接依赖。
- `requirements-dev.txt`：测试、图标处理和 PyInstaller 构建依赖。
- `pyproject.toml`：项目元数据、Python 版本范围和可安装入口。
- `THIRD_PARTY_NOTICES.md`：第三方组件来源与许可提示。

升级依赖时同时更新 `requirements*.txt` 和 `pyproject.toml`，然后在受支持的最低与最高 Python 版本上运行 CI。

## 3. 修改与验证流程

1. 从最新 `main` 创建功能分支。
2. 修改代码并补充对应测试。
3. 运行 `scripts\test.ps1`。
4. 运行 `scripts\check-privacy.ps1`。
5. 查看 `git diff --check` 和 `git status --short`。
6. 功能性改动最后运行 `build.ps1`，并在没有系统 Python 依赖的环境中冷启动 EXE。

真实 Telegram 下载和阿里云盘上传无法在 CI 中自动验收，因为它们需要个人凭据。发布前至少手工验证一次“小视频扫描 → 下载 → FFmpeg 处理 → 上传 → 远端大小校验”。

## 4. 版本发布

版本号需要保持一致：

- `pyproject.toml` 中的 `project.version`
- `telegram_video_downloader/__init__.py` 中的 `__version__`
- `version_info.txt` 中的文件版本和产品版本

构建后产物位于 `dist\telegram视频下载器.exe`。不要将 `dist`、`work`、`.venv`、EXE 或源码 ZIP 直接提交到 Git；发布二进制时使用 GitHub Release 附件。

## 5. 敏感数据规则

仓库中的测试必须使用明显虚构的手机号、聊天 ID、文件名和令牌。禁止提交：

- Telegram `.session` 文件、API Hash、验证码或二步验证密码
- `config.json`、`state.sqlite3`、日志和 OpenList `data` 目录
- 下载视频、`.part` 文件和 `upload_staging`
- Windows 凭据导出、云盘令牌或包含真实用户目录的绝对路径

提交前的隐私脚本会拒绝常见本地数据与媒体扩展名，并检查当前 Windows 用户目录是否被写进已跟踪文本。
