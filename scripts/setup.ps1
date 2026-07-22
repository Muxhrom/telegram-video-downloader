param(
    [switch]$Development
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$venvRoot = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
$setupTemp = Join-Path $projectRoot 'work\setup-temp'

New-Item -ItemType Directory -Force -Path $setupTemp | Out-Null
$env:TEMP = $setupTemp
$env:TMP = $setupTemp

function Remove-BrokenVirtualEnvironment {
    if (-not (Test-Path -LiteralPath $venvRoot)) { return }
    $resolvedProject = [System.IO.Path]::GetFullPath($projectRoot)
    $resolvedVenv = [System.IO.Path]::GetFullPath($venvRoot)
    if (-not $resolvedVenv.StartsWith($resolvedProject, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝删除项目目录之外的虚拟环境：$resolvedVenv"
    }
    if ((Split-Path -Leaf $resolvedVenv) -ne '.venv') {
        throw "拒绝删除非 .venv 目录：$resolvedVenv"
    }
    Remove-Item -LiteralPath $resolvedVenv -Recurse -Force
}

function New-ProjectVirtualEnvironment {
    $launcher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($version in @('3.13', '3.12', '3.11')) {
            $probeArgs = @("-$version", '-c', 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)')
            & $launcher.Source @probeArgs 2>$null
            if ($LASTEXITCODE -eq 0) {
                $createArgs = @("-$version", '-m', 'venv', $venvRoot)
                & $launcher.Source @createArgs
                if ($LASTEXITCODE -ne 0) {
                    throw "使用 Python $version 创建虚拟环境失败，退出码 $LASTEXITCODE"
                }
                return
            }
        }
    }

    $python = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($python) {
        $probeArgs = @('-c', 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)')
        & $python.Source @probeArgs
        if ($LASTEXITCODE -eq 0) {
            $createArgs = @('-m', 'venv', $venvRoot)
            & $python.Source @createArgs
            if ($LASTEXITCODE -ne 0) {
                throw "创建虚拟环境失败，退出码 $LASTEXITCODE"
            }
            return
        }
    }

    throw '未找到受支持的 Python。请安装 64 位 Python 3.11、3.12 或 3.13。'
}

if (Test-Path -LiteralPath $venvPython) {
    $pipProbeArgs = @('-m', 'pip', '--version')
    & $venvPython @pipProbeArgs *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning '检测到不完整的 .venv，正在安全重建。'
        Remove-BrokenVirtualEnvironment
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    New-ProjectVirtualEnvironment
}

$upgradeArgs = @('-m', 'pip', 'install', '--upgrade', 'pip')
& $venvPython @upgradeArgs
if ($LASTEXITCODE -ne 0) { throw "升级 pip 失败，退出码 $LASTEXITCODE" }

$requirementsName = if ($Development) { 'requirements-dev.txt' } else { 'requirements.txt' }
$requirementsPath = Join-Path $projectRoot $requirementsName
$installArgs = @('-m', 'pip', 'install', '-r', $requirementsPath)
& $venvPython @installArgs
if ($LASTEXITCODE -ne 0) { throw "安装依赖失败，退出码 $LASTEXITCODE" }

Write-Host "环境准备完成：$venvPython"
