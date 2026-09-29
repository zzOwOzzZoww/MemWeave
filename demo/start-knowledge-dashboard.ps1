$ErrorActionPreference = 'Stop'
$demoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $demoRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
$database = Join-Path $projectRoot 'data\knowledge.db'
$config = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.memweave\config.json'

try {
    if (-not (Test-Path -LiteralPath $config)) {
        & python -m agent_knowledge_bridge.cli init `
            --project claude-codex-mvp --database $database
        if ($LASTEXITCODE -ne 0) { throw 'memweave init failed' }
    }
    & python -m agent_knowledge_bridge.cli ui
    if ($LASTEXITCODE -ne 0) { throw 'memweave ui failed' }
}
catch {
    $message = "MemWeave knowledge console failed to start.`r`n`r`n$($_.Exception.Message)"
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $message,
        'MemWeave Knowledge Console',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
    exit 1
}
