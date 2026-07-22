$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$git = (Get-Command 'git.exe' -ErrorAction Stop).Source

$trackedFiles = & $git -C $projectRoot ls-files
if ($LASTEXITCODE -ne 0) { throw "读取 Git 文件列表失败，退出码 $LASTEXITCODE" }

$forbiddenPattern = '(?i)(^|/)(tools|upload_staging|dist|work|\.venv|__pycache__)(/|$)|\.(session|log|sqlite|sqlite3|db|tmp|env|exe|zip|mp4|mkv|webm|mov|avi|flv|wmv|m4v|part)$'
$forbidden = $trackedFiles | Where-Object { $_ -match $forbiddenPattern }
if ($forbidden) {
    throw "发现不应提交的本地数据或构建产物：$($forbidden -join ', ')"
}

$profilePath = [string]$env:USERPROFILE
if (-not [string]::IsNullOrWhiteSpace($profilePath)) {
    $grepArgs = @('-C', $projectRoot, 'grep', '-I', '-n', '-F', '--', $profilePath)
    $matches = & $git @grepArgs 2>$null
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        throw "发现当前 Windows 用户目录：$($matches -join '; ')"
    }
    if ($exitCode -ne 1) { throw "隐私内容检查失败，退出码 $exitCode" }
}

Write-Host '隐私检查通过：未跟踪会话、日志、数据库、媒体、构建产物或当前用户绝对路径。'
