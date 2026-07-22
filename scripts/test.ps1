$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$setupScript = Join-Path $projectRoot 'scripts\setup.ps1'

& $setupScript -Development

$testArgs = @('-m', 'pytest', (Join-Path $projectRoot 'tests'))
Push-Location $projectRoot
try {
    & $pythonExe @testArgs
    if ($LASTEXITCODE -ne 0) { throw "测试失败，退出码 $LASTEXITCODE" }
} finally {
    Pop-Location
}
