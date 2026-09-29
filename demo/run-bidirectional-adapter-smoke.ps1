$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
python (Join-Path $projectRoot 'scripts\smoke_bidirectional_hooks.py')
exit $LASTEXITCODE
