$ErrorActionPreference = 'Stop'
$demoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $demoRoot
$workspace = Join-Path $demoRoot 'self-learning-workspace'
$dataRoot = Join-Path $demoRoot 'self-learning-data'
$hookScript = Join-Path $projectRoot 'scripts\claude_learning_hook.py'
$runtimeScript = Join-Path $projectRoot 'scripts\memweave-runtime.ps1'
. $runtimeScript

$env:MEMWEAVE_ROOT = $projectRoot
$env:MW_DB_PATH = Join-Path $dataRoot 'knowledge.db'
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

New-Item -ItemType Directory -Force -Path $dataRoot | Out-Null
Get-ChildItem $dataRoot -File -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem -LiteralPath (Join-Path $workspace 'outputs') -File -ErrorAction SilentlyContinue |
    Where-Object Name -ne '.gitkeep' |
    Remove-Item -Force

$runtime = Start-MemWeaveRuntime `
    -ProjectRoot $projectRoot `
    -DatabasePath $env:MW_DB_PATH `
    -LogDirectory (Join-Path $dataRoot 'runtime-logs')
Write-Host "Runtime: $($runtime.Url)"

function Get-Metrics {
    $raw = & python $hookScript metrics
    if ($LASTEXITCODE -ne 0) { throw 'Could not read MemWeave metrics.' }
    return $raw | ConvertFrom-Json
}

function Wait-LearningRun([int]$ExpectedRuns, [string]$SessionId) {
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        $metrics = Get-Metrics
        if ($metrics.learning.completed_runs -ge $ExpectedRuns) {
            return $metrics
        }
        $errors = Join-Path $dataRoot 'knowledge.hook-errors.jsonl'
        if (Test-Path $errors) { throw "Learning hook failed. See $errors" }
        Start-Sleep -Seconds 1
    }

    # `claude -p` can exit before its asynchronous Stop hook starts. Replay the
    # same hook from the saved transcript to keep this headless check reliable.
    $transcript = Get-ChildItem (Join-Path $env:USERPROFILE '.claude\projects') `
        -Recurse -Filter "$SessionId.jsonl" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $transcript) {
        throw "Could not find Claude transcript for session $SessionId."
    }
    $hookInput = @{
        hook_event_name = 'Stop'
        session_id = $SessionId
        transcript_path = $transcript.FullName
        last_assistant_message = ''
    } | ConvertTo-Json -Compress
    $hookInput | & python $hookScript hook | Out-Null

    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        $metrics = Get-Metrics
        if ($metrics.learning.completed_runs -ge $ExpectedRuns) {
            return $metrics
        }
        $errors = Join-Path $dataRoot 'knowledge.hook-errors.jsonl'
        if (Test-Path $errors) { throw "Learning hook failed. See $errors" }
        Start-Sleep -Seconds 1
    }
    throw "Timed out waiting for learning run $ExpectedRuns."
}

$learnPrompt = Get-Content -Raw (Join-Path $workspace 'tasks\LEARN_TASK.md')
$applyPrompt = Get-Content -Raw (Join-Path $workspace 'tasks\APPLY_TASK.md')

Push-Location $workspace
try {
    $learnSession = [guid]::NewGuid().ToString()
    Write-Host 'Stage 1/2: teach one verified widget workflow'
    & ccb -p $learnPrompt --session-id $learnSession --model deepseek-flash --dangerously-skip-permissions
    if ($LASTEXITCODE -ne 0) { throw 'Learning task failed.' }
    $afterLearn = Wait-LearningRun 1 $learnSession
    if ($afterLearn.knowledge.active -lt 1) {
        throw 'No knowledge was promoted after the verified learning task.'
    }
    $alphaArtifact = Join-Path $workspace 'outputs\alpha.widget.json'
    $alphaEvidence = Join-Path $dataRoot 'alpha.widget.json'
    Move-Item -LiteralPath $alphaArtifact -Destination $alphaEvidence -Force

    $applySession = [guid]::NewGuid().ToString()
    Write-Host 'Stage 2/2: apply the learned workflow in a fresh session'
    & ccb -p $applyPrompt --session-id $applySession --model deepseek-flash --dangerously-skip-permissions
    if ($LASTEXITCODE -ne 0) { throw 'Application task failed.' }
    $afterApply = Wait-LearningRun 2 $applySession

    & python .\tools\verify_widget.py $alphaEvidence
    if ($LASTEXITCODE -ne 0) { throw 'Alpha artifact verification failed.' }
    & python .\tools\verify_widget.py .\outputs\beta.widget.json
    if ($LASTEXITCODE -ne 0) { throw 'Beta artifact verification failed.' }
    Copy-Item -LiteralPath $alphaEvidence -Destination $alphaArtifact -Force
}
finally {
    Pop-Location
    Stop-MemWeaveRuntime -Runtime $runtime
}

Write-Host 'SELF-LEARNING DEMO PASS'
& python $hookScript metrics
Write-Host 'Pending knowledge:'
& python $hookScript pending
