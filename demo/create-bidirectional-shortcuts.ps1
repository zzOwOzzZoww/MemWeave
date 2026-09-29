$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$desktop = [Environment]::GetFolderPath('Desktop')
$terminal = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\wt.exe'
if (-not (Test-Path -LiteralPath $terminal)) {
    throw "Windows Terminal was not found: $terminal"
}

$shell = New-Object -ComObject WScript.Shell

function Set-AdapterShortcut {
    param(
        [Parameter(Mandatory = $true)][string]$ShortcutPath,
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string]$Icon
    )

    Write-Host "Creating shortcut: [$ShortcutPath]"
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $terminal
    $shortcut.Arguments = '-w new nt --title "' + $Title + '" -d "' + $projectRoot + '" powershell.exe -NoExit -ExecutionPolicy Bypass -File "' + $Script + '"'
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.IconLocation = $Icon
    $shortcut.WindowStyle = 1
    $shortcut.Save()
}

$claudeIcon = Join-Path $env:APPDATA 'npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe,0'
if (-not (Test-Path -LiteralPath ($claudeIcon -replace ',0$',''))) {
    $claudeIcon = "$env:SystemRoot\System32\shell32.dll,14"
}
$codexIcon = Join-Path $env:LOCALAPPDATA 'Codex\icon-chatgpt.ico,0'
if (-not (Test-Path -LiteralPath ($codexIcon -replace ',0$',''))) {
    $codexIcon = "$env:SystemRoot\System32\shell32.dll,14"
}

$existingClaude = Get-ChildItem -LiteralPath $desktop -Filter 'Claude*.lnk' -File | Select-Object -First 1
if ($null -eq $existingClaude) {
    $existingClaudePath = Join-Path $desktop 'MemWeave-Claude.lnk'
} else {
    $existingClaudePath = $existingClaude.FullName
}

Set-AdapterShortcut `
    -ShortcutPath $existingClaudePath `
    -Title 'MemWeave Claude Code Adapter' `
    -Script (Join-Path $projectRoot 'demo\start-claude-adapter.ps1') `
    -Icon $claudeIcon

$codexShortcut = $shell.CreateShortcut((Join-Path $desktop 'MemWeave-Codex.lnk'))
$codexShortcut.TargetPath = Join-Path $env:SystemRoot 'System32\wscript.exe'
$codexShortcut.Arguments = '"' + (Join-Path $projectRoot 'demo\launch-codex-desktop.vbs') + '"'
$codexShortcut.WorkingDirectory = $projectRoot
$codexShortcut.IconLocation = $codexIcon
$codexShortcut.WindowStyle = 1
$codexShortcut.Save()

$memWeaveIcon = Join-Path $projectRoot 'assets\memweave-icon.ico'
if (-not (Test-Path -LiteralPath $memWeaveIcon)) {
    $memWeaveIconLocation = "$env:SystemRoot\System32\shell32.dll,14"
} else {
    $memWeaveIconLocation = "$memWeaveIcon,0"
}
$knowledgeShortcut = $shell.CreateShortcut((Join-Path $desktop 'MemWeave知识管理.lnk'))
$knowledgeShortcut.TargetPath = Join-Path $env:SystemRoot 'System32\wscript.exe'
$knowledgeShortcut.Arguments = '"' + (Join-Path $projectRoot 'demo\launch-knowledge-dashboard.vbs') + '"'
$knowledgeShortcut.WorkingDirectory = $projectRoot
$knowledgeShortcut.IconLocation = $memWeaveIconLocation
$knowledgeShortcut.WindowStyle = 1
$knowledgeShortcut.Save()

Write-Host 'Created:'
Write-Host $existingClaudePath
Write-Host (Join-Path $desktop 'MemWeave-Codex.lnk')
Write-Host (Join-Path $desktop 'MemWeave知识管理.lnk')
