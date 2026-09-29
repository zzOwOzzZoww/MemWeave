param(
    [ValidateSet('codex', 'claude')]
    [string]$Agent = 'codex'
)
$ErrorActionPreference = 'Stop'

if ($Agent -eq 'codex') {
    & (Join-Path $PSScriptRoot 'start-codex-desktop.ps1')
    exit $LASTEXITCODE
}

$demoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $demoRoot
. (Join-Path $projectRoot 'scripts\memweave-runtime.ps1')

$env:MEMWEAVE_ROOT = $projectRoot
$env:MW_DB_PATH = Join-Path $projectRoot 'data\knowledge.db'
$env:MW_PROJECT_KEY = 'claude-codex-mvp'
$env:MW_AGENT_ID = if ($Agent -eq 'codex') { 'codex' } else { 'claude-code' }
$env:MW_BASE_URL = 'https://api.deepseek.com/v1'
$env:MW_MODEL = 'deepseek-flash'
$env:PYTHONUTF8 = '1'

$deepSeekKey = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
if ([string]::IsNullOrWhiteSpace($deepSeekKey)) {
    $deepSeekKey = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY', 'User')
}
if ([string]::IsNullOrWhiteSpace($deepSeekKey)) {
    throw 'Missing DEEPSEEK_API_KEY or OPENAI_API_KEY in the Windows user environment.'
}
$env:DEEPSEEK_API_KEY = $deepSeekKey

$runtime = Start-MemWeaveRuntime `
    -ProjectRoot $projectRoot `
    -DatabasePath $env:MW_DB_PATH `
    -LogDirectory (Join-Path $projectRoot 'data\runtime-logs')

Write-Host "MemWeave bidirectional adapter: $Agent"
Write-Host "Shared database: $env:MW_DB_PATH"
Write-Host "Project: $env:MW_PROJECT_KEY"
Write-Host "Runtime: $($runtime.Url)"
Write-Host 'Hooks are automatic; MCP is not required for recall or learning.'

try {
    Push-Location (Join-Path $demoRoot 'self-learning-workspace')
    try {
        & ccb --model deepseek-flash --setting-sources 'project,local'
        $exitCode = $LASTEXITCODE
    } finally { Pop-Location }
} finally {
    Stop-MemWeaveRuntime -Runtime $runtime
}
exit $exitCode
