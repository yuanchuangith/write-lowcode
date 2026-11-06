[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$pluginRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $env:LOCALAPPDATA 'GxpLowcodeEditor\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'GXP MCP runtime is missing. Run scripts\setup.ps1 first.'
}
# Both MCP servers run verified package snapshots and hold independent leases.
& $pythonPath (Join-Path $pluginRoot 'mcp\gxp_editor\runtime_package.py') start --source $pluginRoot --server form
exit $LASTEXITCODE
