$ErrorActionPreference = 'Stop'
$libraryPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $libraryPython)) {
    throw 'Install dependencies first: uv sync --locked --cache-dir .local/uv-cache'
}
$env:SCHOOL_MCP_LOCAL_DIR = Join-Path $PSScriptRoot '.local'
$env:PYTHONUTF8 = '1'
& $libraryPython -m school_mcp library-login
if ($LASTEXITCODE -ne 0) {
    throw 'The library login did not complete. Check .local/library.login-status.json.'
}
