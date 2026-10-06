$ErrorActionPreference = 'Stop'
$jwPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $jwPython)) {
    throw 'Install dependencies first: uv sync --locked --cache-dir .local/uv-cache'
}
$env:SCHOOL_MCP_LOCAL_DIR = Join-Path $PSScriptRoot '.local'
$env:PYTHONUTF8 = '1'
& $jwPython -m school_mcp jw-login
if ($LASTEXITCODE -ne 0) {
    throw 'The academic-system login did not complete. Check .local/jw.login-status.json.'
}
