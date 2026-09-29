$ErrorActionPreference = 'Stop'
$demoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$workdir = Join-Path $demoRoot 'claude-workspace'
$env:AKB_DB_PATH = Join-Path $demoRoot 'data\knowledge.db'

$deepSeekKey = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
if ([string]::IsNullOrWhiteSpace($deepSeekKey)) {
    $deepSeekKey = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY', 'User')
}
if ([string]::IsNullOrWhiteSpace($deepSeekKey)) {
    throw 'Missing DEEPSEEK_API_KEY or OPENAI_API_KEY in the Windows user environment.'
}

$env:CLAUDE_CODE_USE_OPENAI = '1'
$env:OPENAI_API_KEY = $deepSeekKey
$env:OPENAI_BASE_URL = 'https://api.deepseek.com/v1'
$env:OPENAI_MODEL = 'deepseek-flash'
$env:OPENAI_ENABLE_THINKING = '0'

Set-Location $workdir
& ccb --model deepseek-flash --name 'MemWeave-Claude-Publish' `
    'Read PUBLISH_TASK.md and complete every step in stage one.'
exit $LASTEXITCODE
