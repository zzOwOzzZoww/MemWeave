$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'start-bidirectional.ps1') -Agent claude
exit $LASTEXITCODE
