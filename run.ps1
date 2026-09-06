$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'The local environment is missing. Run .\setup.ps1 first.'
}
Set-Location -LiteralPath $projectRoot
& $venvPython app.py
