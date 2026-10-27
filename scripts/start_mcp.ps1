[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$pluginRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $env:LOCALAPPDATA 'GxpLowcodeEditor\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'GXP editor MCP runtime is missing. Run scripts\setup.ps1 first.'
}
# The bootstrap validates and stages the complete package before running the
# server. Deferred imports, UI scripts and Node rules all use that fixed root.
& $pythonPath (Join-Path $pluginRoot 'mcp\gxp_editor\runtime_package.py') start --source $pluginRoot
exit $LASTEXITCODE
