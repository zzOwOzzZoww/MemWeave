$ErrorActionPreference = 'Stop'
$demoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $demoRoot
$workspace = Join-Path $demoRoot 'self-learning-workspace'
$mcpConfig = Join-Path $workspace '.mcp.json'
$runtimeScript = Join-Path $projectRoot 'scripts\memweave-runtime.ps1'
. $runtimeScript

$env:MEMWEAVE_ROOT = $projectRoot
$env:MW_DB_PATH = Join-Path $demoRoot 'self-learning-data\knowledge.db'
$env:MW_PROJECT_KEY = 'claude-self-learning-demo'
$env:MW_AGENT_ID = 'claude-code'
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

$env:CLAUDE_CODE_USE_OPENAI = '1'
$env:OPENAI_API_KEY = $deepSeekKey
$env:OPENAI_BASE_URL = $env:MW_BASE_URL
$env:OPENAI_MODEL = $env:MW_MODEL
$env:OPENAI_ENABLE_THINKING = '0'

$runtime = Start-MemWeaveRuntime `
    -ProjectRoot $projectRoot `
    -DatabasePath $env:MW_DB_PATH `
    -LogDirectory (Join-Path $demoRoot 'self-learning-data\runtime-logs')

Write-Host 'MemWeave self-learning demo is enabled for this Claude Code session.'
Write-Host "Workspace: $workspace"
Write-Host "Database: $env:MW_DB_PATH"
Write-Host "Runtime: $($runtime.Url)"
Write-Host 'Use /new between related tasks to test cross-session recall.'

Push-Location $workspace
try {
    & ccb --model deepseek-flash --name 'MemWeave-Self-Learning' `
        --mcp-config $mcpConfig --strict-mcp-config `
        --setting-sources 'project,local'
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    Stop-MemWeaveRuntime -Runtime $runtime
}
exit $exitCode
