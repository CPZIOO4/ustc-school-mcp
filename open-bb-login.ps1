$ErrorActionPreference = 'Stop'
$bbPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $bbPython)) {
    throw 'Install dependencies first: uv sync --locked --cache-dir .local/uv-cache'
}
$env:SCHOOL_MCP_LOCAL_DIR = Join-Path $PSScriptRoot '.local'
$env:PYTHONUTF8 = '1'
& $bbPython -m school_mcp bb-login
if ($LASTEXITCODE -ne 0) {
    throw 'The BB login window could not complete. Check .local/bb.login-status.json.'
}
