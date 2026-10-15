$ErrorActionPreference = 'Stop'
$workspace = 'C:\Users\yuanchuan\Desktop\git\low_code\write'
$jobRoot = Join-Path $workspace 'output\codex-reinstall'
$stage = (Resolve-Path -LiteralPath (Join-Path $jobRoot 'gxp-lowcode-editor')).ProviderPath
$marketRoot = (Resolve-Path -LiteralPath 'C:\Users\yuanchuan\Desktop\git\bianpanMcpAndSkill').ProviderPath
$target = (Resolve-Path -LiteralPath (Join-Path $marketRoot 'plugins\gxp-lowcode-editor')).ProviderPath
$expectedTarget = [IO.Path]::GetFullPath((Join-Path $marketRoot 'plugins\gxp-lowcode-editor'))
if ($target -ne $expectedTarget -or -not $stage.StartsWith($workspace + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Unexpected installation source or target path.'
}
$manifest = Get-Content -LiteralPath (Join-Path $stage '.codex-plugin\plugin.json') -Raw | ConvertFrom-Json
if ($manifest.name -ne 'gxp-lowcode-editor' -or $manifest.version -ne '0.1.11+codex.local-20261015-canvas-native-icons') {
    throw 'Unexpected plugin package.'
}
$backup = [IO.Path]::GetFullPath((Join-Path $jobRoot ('previous-marketplace-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))))
if (-not $backup.StartsWith($workspace + '\', [StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath $backup)) {
    throw 'Unexpected or existing backup path.'
}
Copy-Item -LiteralPath $target -Destination $backup -Recurse -Force
Write-Output ('Previous source backed up to: ' + $backup)
foreach ($entry in Get-ChildItem -LiteralPath $stage -Force) {
    Copy-Item -LiteralPath $entry.FullName -Destination $target -Recurse -Force
}
$codexExe = 'C:\Users\yuanchuan\.vscode\extensions\openai.chatgpt-26.1002.51308-win32-x64\bin\windows-x86_64\codex.exe'
$result = & $codexExe plugin add 'gxp-lowcode-editor@bianpan-local' --json
if ($LASTEXITCODE -ne 0) { throw 'Codex plugin installation failed.' }
$result | Set-Content -LiteralPath (Join-Path $jobRoot 'install-result.json') -Encoding UTF8
Write-Output ($result -join "`n")
$cache = Join-Path 'C:\Users\yuanchuan\.codex\plugins\cache\bianpan-local\gxp-lowcode-editor' $manifest.version
if (-not (Test-Path -LiteralPath $cache)) { throw 'The expected installed cache was not created.' }
$verified = 0
foreach ($file in Get-ChildItem -LiteralPath $stage -Recurse -File -Force) {
    $relative = $file.FullName.Substring($stage.Length + 1)
    $installedFile = Join-Path $cache $relative
    if (-not (Test-Path -LiteralPath $installedFile)) { throw ('Installed file is missing: ' + $relative) }
    if ((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $installedFile -Algorithm SHA256).Hash) {
        throw ('Installed file differs: ' + $relative)
    }
    $verified++
}
Write-Output ('Installed version: ' + $manifest.version)
Write-Output ('Installed cache: ' + $cache)
Write-Output ('Verified package files: ' + $verified)
