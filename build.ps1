$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$buildTemp = Join-Path $projectRoot 'work\build-temp'
$setupScript = Join-Path $projectRoot 'scripts\setup.ps1'

Write-Host '预计构建耗时 3–10 分钟，首次安装依赖时可能更久。'
& $setupScript -Development
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "依赖安装完成后仍未找到虚拟环境 Python：$pythonExe"
}

New-Item -ItemType Directory -Path $buildTemp -Force | Out-Null
$env:TEMP = $buildTemp
$env:TMP = $buildTemp

$testArgs = @(
    '-m'
    'pytest'
    (Join-Path $projectRoot 'tests')
    '--basetemp'
    (Join-Path $buildTemp 'pytest')
    '-p'
    'no:cacheprovider'
)
Push-Location $projectRoot
try {
    & $pythonExe @testArgs
    if ($LASTEXITCODE -ne 0) { throw "测试失败，退出码 $LASTEXITCODE" }
} finally {
    Pop-Location
}

$outputDir = Join-Path $projectRoot 'dist'
$pyInstallerWork = Join-Path $projectRoot 'work\pyinstaller'
$pyInstallerSpec = Join-Path $projectRoot 'work\pyinstaller-spec'
$buildArgs = @(
    '-m'
    'PyInstaller'
    '--onefile'
    '--windowed'
    '--noconfirm'
    '--clean'
    '--name=telegram视频下载器'
    '--collect-submodules=keyring.backends'
    '--collect-submodules=python_socks'
    '--hidden-import=keyring.backends.Windows'
    "--icon=$(Join-Path $projectRoot 'assets\app.ico')"
    "--add-data=$(Join-Path $projectRoot 'assets\app-icon.png');assets"
    "--version-file=$(Join-Path $projectRoot 'version_info.txt')"
    "--distpath=$outputDir"
    "--workpath=$pyInstallerWork"
    "--specpath=$pyInstallerSpec"
    (Join-Path $projectRoot 'run.py')
)
& $pythonExe @buildArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建 EXE 失败，退出码 $LASTEXITCODE" }

$exePath = Join-Path $outputDir 'telegram视频下载器.exe'
if (-not (Test-Path -LiteralPath $exePath)) { throw "未找到构建产物 $exePath" }
Write-Host "构建完成：$exePath"
