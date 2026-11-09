[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$pluginRoot = Split-Path -Parent $PSScriptRoot
$settingsScript = Join-Path $pluginRoot 'scripts\database_settings.py'
$pythonPath = $null

# Prefer the existing plugin runtime, which already contains its dependencies.
if ($env:LOCALAPPDATA) {
    foreach ($runtime in @('GxpLowcodeReadonly', 'GxpLowcodeEditor')) {
        $candidate = Join-Path $env:LOCALAPPDATA "$runtime\.venv\Scripts\pythonw.exe"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $pythonPath = $candidate
            break
        }
    }
}
if (-not $pythonPath) {
    $command = Get-Command pythonw.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($command) { $pythonPath = $command.Source }
}
if (-not $pythonPath) {
    $command = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($command) {
        $candidate = Join-Path (Split-Path -Parent $command.Source) 'pythonw.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $pythonPath = $candidate }
    }
}
if (-not $pythonPath) {
    throw 'Python GUI runtime is missing. Install Look or Write first, then retry.'
}

Start-Process -FilePath $pythonPath -ArgumentList @('"' + $settingsScript + '"') -WindowStyle Hidden
