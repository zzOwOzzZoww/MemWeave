$ErrorActionPreference = 'Stop'
$demoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$claudeWorkdir = Join-Path $demoRoot 'claude-workspace'
$codexWorkdir = Join-Path $demoRoot 'codex-workspace'
$dataDirectory = Join-Path $demoRoot 'data'
$controller = Join-Path $demoRoot 'scripts\demo_control.py'
$env:AKB_DB_PATH = Join-Path $demoRoot 'data\knowledge.db'
$databaseForToml = $env:AKB_DB_PATH.Replace('\', '/')
$mcpDatabaseOverride = 'mcp_servers.agent_knowledge_bridge.env.AKB_DB_PATH="' + $databaseForToml + '"'
$codexBinRoot = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
$codexBinDirectories = Get-ChildItem $codexBinRoot -Directory -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty FullName
if ($codexBinDirectories.Count -gt 0) {
    $env:PATH = ($codexBinDirectories -join [IO.Path]::PathSeparator) +
        [IO.Path]::PathSeparator + $env:PATH
}
$codexCommand = Get-Command codex.exe -ErrorAction SilentlyContinue
if ($null -ne $codexCommand) {
    $codexExecutable = $codexCommand.Source
}
else {
    $codexExecutable = Get-ChildItem `
        $codexBinRoot `
        -Recurse -Filter codex.exe -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}
if ([string]::IsNullOrWhiteSpace($codexExecutable)) {
    throw 'Codex CLI was not found. Install or update the Codex desktop app first.'
}

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

& python $controller reset
if ($LASTEXITCODE -ne 0) { throw 'Demo reset failed.' }

Push-Location $claudeWorkdir
try {
    & ccb -p 'Read PUBLISH_TASK.md and complete every step in stage one.' `
        --model deepseek-flash --dangerously-skip-permissions
    if ($LASTEXITCODE -ne 0) { throw 'Claude Code publish stage failed.' }
}
finally {
    Pop-Location
}

& $codexExecutable -c $mcpDatabaseOverride -c 'model_reasoning_effort="low"' `
    --disable plugins --disable apps --disable browser_use --disable hooks `
    --disable skill_search exec -C $codexWorkdir --add-dir $dataDirectory `
    --skip-git-repo-check --approve-for-me `
    'Read TASK.md and complete every step in stage two.'
if ($LASTEXITCODE -ne 0) { throw 'Codex handoff stage failed.' }

Push-Location $claudeWorkdir
try {
    & ccb -p 'Read ACCEPT_TASK.md and complete every step in stage three.' `
        --model deepseek-flash --dangerously-skip-permissions --add-dir $demoRoot
    if ($LASTEXITCODE -ne 0) { throw 'Claude Code acceptance stage failed.' }
}
finally {
    Pop-Location
}

& python $controller verify
if ($LASTEXITCODE -ne 0) { throw 'Demo final verification failed.' }
& python $controller status
