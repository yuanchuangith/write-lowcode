[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$sessionScript = Join-Path $PSScriptRoot 'manage_project_sessions.py'
$sessionPython = Join-Path $env:LOCALAPPDATA 'GxpLowcodeEditor\.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $sessionPython -PathType Leaf)) {
    $sessionCommand = Get-Command python.exe -CommandType Application -ErrorAction Stop
    $sessionPython = Join-Path (Split-Path -Parent $sessionCommand.Source) 'pythonw.exe'
}
if (-not (Test-Path -LiteralPath $sessionPython -PathType Leaf)) { throw 'Python GUI runtime is missing.' }
Start-Process -FilePath $sessionPython -ArgumentList @('"' + $sessionScript + '"') -WindowStyle Hidden
