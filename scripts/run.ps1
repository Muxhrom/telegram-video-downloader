$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$setupScript = Join-Path $projectRoot 'scripts\setup.ps1'

$environmentReady = $false
if (Test-Path -LiteralPath $pythonExe) {
    $probeArgs = @('-c', 'import PySide6, telethon, cryptg, python_socks, keyring, httpx')
    & $pythonExe @probeArgs 2>$null
    $environmentReady = $LASTEXITCODE -eq 0
}

if (-not $environmentReady) {
    & $setupScript
}

$runScript = Join-Path $projectRoot 'run.py'
& $pythonExe $runScript
if ($LASTEXITCODE -ne 0) { throw "程序退出，退出码 $LASTEXITCODE" }
