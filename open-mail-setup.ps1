$ErrorActionPreference = 'Stop'
$setupScript = Join-Path $PSScriptRoot 'setup_mail.py'
$pythonCommand = Get-Command python -ErrorAction Stop
& $pythonCommand.Source $setupScript
if ($LASTEXITCODE -ne 0) {
    throw 'The mail setup window could not start. Use a Python installation with tkinter.'
}
