param(
    [int]$Port = 8765,
    [string]$DatabasePath = '',
    [string]$Token = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:MW_DB_PATH = if ($DatabasePath) {
    $DatabasePath
} else {
    Join-Path $projectRoot 'data\knowledge.db'
}
$env:MW_DAEMON_TOKEN = if ($Token) { $Token } else { [guid]::NewGuid().ToString('N') }

Write-Host "MemWeave Runtime: http://127.0.0.1:$Port"
Write-Host "Database: $env:MW_DB_PATH"
Write-Host 'Retrieval: fts5-enriched'
# The token is deliberately not printed: terminal scrollback, CI logs and
# screen shares would all capture a live credential.
& python -m agent_knowledge_bridge.daemon --port $Port
exit $LASTEXITCODE
