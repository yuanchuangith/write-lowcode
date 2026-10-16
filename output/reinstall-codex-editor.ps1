$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath 'C:\Users\yuanchuan\Desktop\git\low_code\write').Path
$marketRoot = (Resolve-Path -LiteralPath 'C:\Users\yuanchuan\Desktop\git\bianpanMcpAndSkill').Path
$pluginTarget = [IO.Path]::GetFullPath((Join-Path $marketRoot 'plugins\gxp-lowcode-editor'))
$expectedTarget = 'C:\Users\yuanchuan\Desktop\git\bianpanMcpAndSkill\plugins\gxp-lowcode-editor'
if ($pluginTarget -ne $expectedTarget) { throw 'Unexpected plugin installation target' }
$targetItem = Get-Item -LiteralPath $pluginTarget
if ($targetItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Plugin target must not be a link' }
$sourceManifest = Get-Content -LiteralPath (Join-Path $projectRoot '.codex-plugin\plugin.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($sourceManifest.name -ne 'gxp-lowcode-editor') { throw 'Unexpected source plugin identity' }
$backupRoot = Join-Path $projectRoot ('output\installation-backups\gxp-lowcode-editor-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
$entries = @('.codex-plugin', '.mcp.json', 'skills', 'mcp', 'scripts', 'assets', 'requirements.txt')
$copied = 0
foreach ($entry in $entries) {
    $sourceEntry = Join-Path $projectRoot $entry
    $sourceItem = Get-Item -LiteralPath $sourceEntry
    if ($sourceItem.PSIsContainer) { $files = @(Get-ChildItem -LiteralPath $sourceEntry -File -Recurse -Force | Where-Object { $_.FullName -notmatch '__pycache__' -and $_.Extension -ne '.pyc' }) }
    else { $files = @($sourceItem) }
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($projectRoot.Length).TrimStart('\', '/')
        $destination = [IO.Path]::GetFullPath((Join-Path $pluginTarget $relative))
        if (-not $destination.StartsWith($pluginTarget + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'File escapes plugin target' }
        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            $backup = Join-Path $backupRoot $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
            Copy-Item -LiteralPath $destination -Destination $backup -Force
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
        if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash) { throw ('Copied file verification failed: ' + $relative) }
        $copied++
    }
}
Write-Output ('Synchronized plugin version: ' + $sourceManifest.version)
Write-Output ('Verified files: ' + $copied)
Write-Output ('Backup: ' + $backupRoot)
$codexPath = 'C:\Users\yuanchuan\.vscode\extensions\openai.chatgpt-26.1002.51308-win32-x64\bin\windows-x86_64\codex.exe'
& $codexPath plugin add 'gxp-lowcode-editor@bianpan-local' --json
if ($LASTEXITCODE -ne 0) { throw ('Codex plugin install failed with exit code ' + $LASTEXITCODE) }
