$ErrorActionPreference = 'Stop'
$demoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$workdir = Join-Path $demoRoot 'codex-workspace'
$dataDirectory = Join-Path $demoRoot 'data'
$env:AKB_DB_PATH = Join-Path $demoRoot 'data\knowledge.db'
$databaseForToml = $env:AKB_DB_PATH.Replace('\', '/')
$mcpDatabaseOverride = 'mcp_servers.agent_knowledge_bridge.env.AKB_DB_PATH="' + $databaseForToml + '"'
$codexBinRoot = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
$codexBinDirectories = Get-ChildItem $codexBinRoot -Directory -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty FullName
if ($codexBinDirectories.Count -gt 0) {
    $env:PATH = ($codexBinDirectories -join [IO.Path]::PathSeparator) +
        [IO.Path]::PathSeparator + $env:PATH
}
$codexCommand = Get-Command codex.exe -ErrorAction SilentlyContinue
if ($null -ne $codexCommand) {
    $codexExecutable = $codexCommand.Source
}
else {
    $codexExecutable = Get-ChildItem `
        $codexBinRoot `
        -Recurse -Filter codex.exe -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}
if ([string]::IsNullOrWhiteSpace($codexExecutable)) {
    throw 'Codex CLI was not found. Install or update the Codex desktop app first.'
}

& $codexExecutable -c $mcpDatabaseOverride -c 'model_reasoning_effort="low"' `
    --disable plugins --disable apps --disable browser_use --disable hooks `
    --disable skill_search -C $workdir --add-dir $dataDirectory --approve-for-me `
    'Read TASK.md and complete every step in stage two.'
exit $LASTEXITCODE
