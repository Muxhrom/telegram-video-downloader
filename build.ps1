$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$buildTemp = Join-Path $projectRoot 'work\build-temp'
New-Item -ItemType Directory -Path $buildTemp -Force | Out-Null
$env:TEMP = $buildTemp
$env:TMP = $buildTemp

if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pythonLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($pythonLauncher) {
        $systemPython = $pythonLauncher.Source
        $nativeArgs = @('-3.13', '-m', 'venv', (Join-Path $projectRoot '.venv'))
    } else {
        $pythonCommand = Get-Command 'python.exe' -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw '未找到 Python 3.13；请先安装 Python 并确保 py.exe 或 python.exe 可用。'
        }
        $systemPython = $pythonCommand.Source
        $nativeArgs = @('-m', 'venv', (Join-Path $projectRoot '.venv'))
    }
    & $systemPython @nativeArgs
    if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败，退出码 $LASTEXITCODE" }
}

$nativeArgs = @('-m', 'pip', 'install', '-r', (Join-Path $projectRoot 'requirements-dev.txt'))
& $pythonExe @nativeArgs
if ($LASTEXITCODE -ne 0) { throw "安装依赖失败，退出码 $LASTEXITCODE" }

$nativeArgs = @(
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
    & $pythonExe @nativeArgs
    if ($LASTEXITCODE -ne 0) { throw "测试失败，退出码 $LASTEXITCODE" }
} finally {
    Pop-Location
}

$outputDir = Join-Path $projectRoot 'dist'
$pyInstallerWork = Join-Path $projectRoot 'work\pyinstaller'
$pyInstallerSpec = Join-Path $projectRoot 'work\pyinstaller-spec'
$nativeArgs = @(
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
& $pythonExe @nativeArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建 EXE 失败，退出码 $LASTEXITCODE" }

$exePath = Join-Path $outputDir 'telegram视频下载器.exe'
if (-not (Test-Path -LiteralPath $exePath)) { throw "未找到构建产物 $exePath" }
Write-Host "构建完成：$exePath"
