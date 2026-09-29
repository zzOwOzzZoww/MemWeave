$ErrorActionPreference = 'Stop'

$demoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $demoRoot
$workspace = Join-Path $demoRoot 'codex-workspace'
$dataRoot = Join-Path $projectRoot 'data\runtime'
$database = Join-Path $projectRoot 'data\knowledge.db'
$statePath = Join-Path $dataRoot 'runtime-state.json'
$errorLog = Join-Path $dataRoot 'codex-launcher-error.log'
$statusLog = Join-Path $dataRoot 'codex-launcher-status.log'
$runtimeScript = Join-Path $projectRoot 'scripts\memweave-runtime.ps1'

New-Item -ItemType Directory -Force -Path $dataRoot | Out-Null
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'

function Test-RuntimeState {
    param($State)

    if ($null -eq $State -or -not $State.url -or -not $State.token -or -not $State.pid) {
        return $false
    }
    $process = Get-Process -Id ([int]$State.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process -or $process.ProcessName -ne 'python') {
        return $false
    }
    try {
        $health = Invoke-RestMethod -Uri "$($State.url)/v1/health" `
            -Headers @{ Authorization = "Bearer $($State.token)" } -TimeoutSec 2
        # Any 0.4.x runtime is acceptable: pinning the exact patch level here
        # silently killed and restarted a healthy daemon after a version bump.
        return $health.status -eq 'ok' -and $health.version -like '0.4.*' `
            -and $health.retrieval -eq 'fts5-enriched'
    }
    catch {
        return $false
    }
}

function Find-CodexLauncher {
    $binRoot = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
    $candidate = Get-ChildItem -LiteralPath $binRoot -Recurse -Filter 'codex.exe' `
        -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1 -ExpandProperty FullName
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        throw 'Codex Desktop launcher was not found. Install or update the official Codex app.'
    }
    return $candidate
}

try {
    $state = $null
    if (Test-Path -LiteralPath $statePath) {
        try {
            $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        catch {
            $state = $null
        }
    }

    $reused = Test-RuntimeState -State $state
    if (-not $reused) {
        if ($null -ne $state -and $state.pid) {
            $runtimeProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$state.pid)" -ErrorAction SilentlyContinue
            if ($null -ne $runtimeProcess -and $runtimeProcess.CommandLine -match 'agent_knowledge_bridge\.daemon') {
                Stop-Process -Id ([int]$state.pid) -Force -ErrorAction SilentlyContinue
            }
        }
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue

        . $runtimeScript
        $runtime = Start-MemWeaveRuntime `
            -ProjectRoot $projectRoot `
            -DatabasePath $database `
            -LogDirectory (Join-Path $dataRoot 'logs')
        $state = [pscustomobject]@{
            pid = $runtime.Process.Id
            url = $runtime.Url
            token = $runtime.Token
            # Must match the key the hooks and the CLI read.  This launcher
            # wrote 'database' while every reader looked for 'database_path',
            # so its recorded path was silently ignored and the hook fell back
            # to a default that need not have been the same file.
            database_path = $database
            project_key = 'claude-codex-mvp'
            started_at = (Get-Date).ToString('o')
        }
        $state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
    }

    $codex = Find-CodexLauncher
    $launcher = Start-Process -FilePath $codex `
        -ArgumentList @('app', $workspace) -WindowStyle Hidden -PassThru
    $launcher.WaitForExit(15000) | Out-Null
    if (-not $launcher.HasExited) {
        throw 'Codex Desktop launcher did not return within 15 seconds.'
    }
    if ($launcher.ExitCode -ne 0) {
        throw "Codex Desktop launcher exited with code $($launcher.ExitCode)."
    }

    @(
        "time=$((Get-Date).ToString('o'))"
        "runtime_pid=$($state.pid)"
        "runtime_url=$($state.url)"
        "runtime_reused=$reused"
        "workspace=$workspace"
        'client=official-codex-desktop'
    ) | Set-Content -LiteralPath $statusLog -Encoding UTF8
    Remove-Item -LiteralPath $errorLog -Force -ErrorAction SilentlyContinue
}
catch {
    $message = "MemWeave Codex Desktop failed to start.`r`n`r`n$($_.Exception.Message)`r`n`r`nLog: $errorLog"
    $message | Set-Content -LiteralPath $errorLog -Encoding UTF8
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $message,
        'MemWeave Codex Desktop',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
    exit 1
}
