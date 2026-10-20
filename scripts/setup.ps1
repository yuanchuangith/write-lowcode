[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$pluginRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'GxpLowcodeEditor'
$venvPath = Join-Path $runtimeRoot '.venv'
$pythonPath = Join-Path $venvPath 'Scripts\python.exe'

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Virtual environment creation failed (exit code $LASTEXITCODE)."
    }
}

& $pythonPath -m pip install --disable-pip-version-check --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "pip upgrade failed (exit code $LASTEXITCODE)."
}
& $pythonPath -m pip install --disable-pip-version-check -r (Join-Path $pluginRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed (exit code $LASTEXITCODE)."
}
Write-Host "Runtime ready: $venvPath"
Write-Host 'Draft writes require a dedicated least-privilege account or fingerprint-pinned shared account, required indexes, and a verified compiler bridge. Nonblank candidate C# must compile against read-only local references (development build or deployment snapshot).'
Write-Host 'Development references establish local compatibility only; production compatibility requires separate deployment and runtime acceptance evidence.'
