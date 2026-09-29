function Start-MemWeaveRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$DatabasePath,
        [Parameter(Mandatory = $true)][string]$LogDirectory,
        # Extra environment entries for the daemon process. A vertical instance
        # that owns a status file on disk passes its path here: the daemon is a
        # separate process, so variables set in the caller's shell never reach it.
        [hashtable]$Environment = @{},
        # Module to launch. The engine's own daemon by default; a vertical
        # instance passes its own entry point, which wraps this app and adds its
        # domain routes on the same origin.
        [string]$Module = 'agent_knowledge_bridge.daemon'
    )

    New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback, 0
    )
    $listener.Start()
    $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()

    # Prepend rather than replace: a vertical instance launches its own module
    # through here and keeps its own src on the path, engine tree first.
    $engineSrc = Join-Path $ProjectRoot 'src'
    $env:PYTHONPATH = if ($env:PYTHONPATH) { "$engineSrc;$env:PYTHONPATH" } else { $engineSrc }
    $env:MW_DB_PATH = $DatabasePath
    $env:MW_DAEMON_TOKEN = [guid]::NewGuid().ToString('N')
    $env:MW_DAEMON_URL = "http://127.0.0.1:$port"
    foreach ($name in $Environment.Keys) {
        Set-Item -Path "Env:$name" -Value ([string]$Environment[$name])
    }

    $stdout = Join-Path $LogDirectory 'runtime.stdout.log'
    $stderr = Join-Path $LogDirectory 'runtime.stderr.log'
    $process = Start-Process -FilePath 'python' `
        -ArgumentList @('-m', $Module, '--port', "$port") `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr

    $headers = @{ Authorization = "Bearer $env:MW_DAEMON_TOKEN" }
    for ($attempt = 0; $attempt -lt 100; $attempt++) {
        if ($process.HasExited) {
            $detail = if (Test-Path $stderr) { Get-Content -Raw $stderr } else { '' }
            throw "MemWeave Runtime exited during startup. $detail"
        }
        try {
            $health = Invoke-RestMethod -Uri "$env:MW_DAEMON_URL/v1/health" `
                -Headers $headers -TimeoutSec 1
            if ($health.status -eq 'ok') {
                return [pscustomobject]@{
                    Process = $process
                    Url = $env:MW_DAEMON_URL
                    Token = $env:MW_DAEMON_TOKEN
                    Stdout = $stdout
                    Stderr = $stderr
                }
            }
        }
        catch {
            Start-Sleep -Milliseconds 100
        }
    }
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw "Timed out waiting for MemWeave Runtime. See $stderr"
}

function Stop-MemWeaveRuntime {
    param([Parameter(Mandatory = $true)]$Runtime)
    if ($null -ne $Runtime.Process -and -not $Runtime.Process.HasExited) {
        Stop-Process -Id $Runtime.Process.Id -Force -ErrorAction SilentlyContinue
        $Runtime.Process.WaitForExit(5000) | Out-Null
    }
    Remove-Item Env:MW_DAEMON_URL -ErrorAction SilentlyContinue
    Remove-Item Env:MW_DAEMON_TOKEN -ErrorAction SilentlyContinue
}

