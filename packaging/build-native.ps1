param(
    [string]$MsvcSetup = '',
    [switch]$SkipEngine,
    [switch]$DebugBuild
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $projectRoot
try {
    if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
        $portable = Join-Path $projectRoot '.local/native-toolchain'
        if (-not (Test-Path -LiteralPath "$portable/cargo/bin/cargo.exe")) { throw 'Install Rust (MSVC) first.' }
        $env:CARGO_HOME = "$portable/cargo"
        $env:RUSTUP_HOME = "$portable/rustup"
        $env:PATH = "$portable/cargo/bin;" + $env:PATH
    }
    if ($MsvcSetup) {
        # Import compiler environment only. File operations remain in PowerShell.
        $setup = (Resolve-Path -LiteralPath $MsvcSetup).Path
        if ($setup.Contains('"')) { throw 'Invalid compiler setup path.' }
        $compilerEnv = & cmd /d /c "call `"$setup`" >nul && set"
        if ($LASTEXITCODE -ne 0) { throw 'Could not load compiler environment.' }
        foreach ($line in $compilerEnv) {
            if ($line -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process') }
        }
    }
    if (-not $SkipEngine) {
        uv --cache-dir .local/uv-desktop-cache sync --locked --group build
        if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
        $previous = $env:SCHOOL_BUILD_NATIVE_ENGINE
        try {
            $env:SCHOOL_BUILD_NATIVE_ENGINE = '1'
            & '.venv/Scripts/python.exe' -m PyInstaller --noconfirm 'packaging/school-agent.spec'
            if ($LASTEXITCODE -ne 0) { throw 'Business engine build failed.' }
        } finally { $env:SCHOOL_BUILD_NATIVE_ENGINE = $previous }
    }
    $engine = Join-Path $projectRoot 'dist/USTC-School-Engine'
    if (-not (Test-Path -LiteralPath "$engine/USTC-School-Engine.exe")) { throw 'Missing business engine.' }
    # Assemble in a fresh directory so removed files from older releases cannot leak.
    $staging = Join-Path $projectRoot ('dist/native-stage-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $staging | Out-Null
    Copy-Item -LiteralPath $engine -Destination (Join-Path $staging 'engine') -Recurse
    $cargoArgs = @('build', '--locked', '--manifest-path', 'native/Cargo.toml')
    if (-not $DebugBuild) { $cargoArgs += '--release' }
    & cargo @cargoArgs
    if ($LASTEXITCODE -ne 0) { throw 'Native client build failed.' }
    $profile = if ($DebugBuild) {'debug'} else {'release'}
    $coreArgs = @('build', '--locked', '--manifest-path', 'native/core/Cargo.toml')
    if (-not $DebugBuild) { $coreArgs += '--release' }
    & cargo @coreArgs
    if ($LASTEXITCODE -ne 0) { throw 'Rust core build failed.' }
    Copy-Item -LiteralPath "native/core/target/$profile/school-core.exe" -Destination "$staging/school-core.exe"
    Copy-Item -LiteralPath "native/target/$profile/ustc-school-desktop.exe" -Destination "$staging/USTC-School-Agent.exe"
    foreach ($file in @('LICENSE', 'docs/native-desktop.md', 'docs/rust-migration.md', 'docs/safety-logs.md')) { Copy-Item -LiteralPath $file -Destination $staging }
    $binaries = [ordered]@{}
    foreach ($relative in @('USTC-School-Agent.exe', 'school-core.exe', 'engine/USTC-School-Engine.exe')) {
        $binaries[$relative] = (Get-FileHash -LiteralPath (Join-Path $staging $relative) -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    [ordered]@{built_at_utc=[DateTime]::UtcNow.ToString('o'); profile=$profile; sha256=$binaries} |
        ConvertTo-Json -Depth 4 | Set-Content -LiteralPath "$staging/release-manifest.json" -Encoding utf8
    $zip = Join-Path $projectRoot 'dist/USTC-School-Agent-Native-Windows.zip'
    Compress-Archive -LiteralPath (Get-ChildItem -LiteralPath $staging | Select-Object -ExpandProperty FullName) -DestinationPath $zip -Force
    (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant() | Set-Content "$zip.sha256"
    Write-Output "Native package: $zip"
    Write-Output "Unpacked application: $staging/USTC-School-Agent.exe"
} finally { Pop-Location }
