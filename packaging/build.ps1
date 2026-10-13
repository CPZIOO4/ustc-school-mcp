$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $projectRoot
try {
    uv --cache-dir .local/uv-desktop-cache sync --locked --group build
    if ($LASTEXITCODE -ne 0) { throw 'Build dependencies could not be installed.' }
    & '.venv/Scripts/python.exe' -m PyInstaller --noconfirm 'packaging/school-agent.spec'
    if ($LASTEXITCODE -ne 0) { throw 'Application build failed.' }
    $releasePath = Join-Path $projectRoot 'dist/USTC-School-Agent'
    Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination $releasePath
    Copy-Item -LiteralPath (Join-Path $projectRoot 'docs/desktop-quickstart.txt') -Destination $releasePath
    Copy-Item -LiteralPath (Join-Path $projectRoot 'docs/safety-logs.md') -Destination $releasePath
    Copy-Item -LiteralPath (Join-Path $projectRoot 'docs/campus-digest.md') -Destination $releasePath
    Compress-Archive -LiteralPath $releasePath -DestinationPath (Join-Path $projectRoot 'dist/USTC-School-Agent-Windows.zip') -Force
} finally { Pop-Location }
