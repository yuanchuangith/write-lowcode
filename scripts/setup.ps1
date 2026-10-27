[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$pluginRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'GxpLowcodeEditor'
$venvPath = Join-Path $runtimeRoot '.venv'
$pythonPath = Join-Path $venvPath 'Scripts\python.exe'

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
# Use the external bootstrap Python: the protected environment itself must not
# be modified until every active or unknown lease has been checked. The same
# OS coordination lock remains held through venv creation and both pip calls.
python (Join-Path $pluginRoot 'mcp\gxp_editor\runtime_package.py') setup --source $pluginRoot --interpreter $pythonPath
if ($LASTEXITCODE -ne 0) {
    throw "Runtime setup refused or failed (exit code $LASTEXITCODE). Close active MCP/UI instances or prepare a separate environment."
}
Write-Host "Runtime ready: $venvPath"
Write-Host 'Draft writes require a dedicated least-privilege account or fingerprint-pinned shared account, required indexes, and a verified compiler bridge. Nonblank candidate C# must compile against read-only local references (development build or deployment snapshot).'
Write-Host 'Development references establish local compatibility only; production compatibility requires separate deployment and runtime acceptance evidence.'
