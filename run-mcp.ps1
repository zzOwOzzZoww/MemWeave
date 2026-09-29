param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')]
    [string]$AgentId,

    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')]
    [string]$ProjectKey = 'claude-codex-mvp'
)

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:AKB_AGENT_ID = $AgentId
$env:AKB_PROJECT_KEY = $ProjectKey
$env:PYTHONPATH = Join-Path $projectRoot 'src'

& python -m agent_knowledge_bridge.server
exit $LASTEXITCODE
