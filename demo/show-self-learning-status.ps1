$ErrorActionPreference = 'Stop'
$demoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $demoRoot
$dataRoot = Join-Path $demoRoot 'self-learning-data'
$hookScript = Join-Path $projectRoot 'scripts\claude_learning_hook.py'

$env:MEMWEAVE_ROOT = $projectRoot
$env:MW_DB_PATH = Join-Path $dataRoot 'knowledge.db'
$env:MW_PROJECT_KEY = 'claude-self-learning-demo'
$env:MW_AGENT_ID = 'claude-code'
$env:PYTHONUTF8 = '1'

Write-Host 'MemWeave metrics:'
& python $hookScript metrics
Write-Host 'Pending knowledge:'
& python $hookScript pending
