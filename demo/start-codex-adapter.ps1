$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'start-codex-desktop.ps1')
exit $LASTEXITCODE
