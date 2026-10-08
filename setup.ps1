$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

function Find-Python {
    $command = Get-Command python3.13, python, py -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }

    $registryPath = 'HKCU:\Software\Python\PythonCore\3.13\InstallPath'
    if (Test-Path $registryPath) {
        $registered = (Get-ItemProperty $registryPath).ExecutablePath
        if ($registered -and (Test-Path -LiteralPath $registered)) { return $registered }
    }
    throw 'Python 3.13 was not found. Install Python, then rerun setup.ps1.'
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    $python = Find-Python
    & $python -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python could not create the local environment. Check your Python installation, then rerun setup.ps1.' }
}

$repairMessage = 'The existing .venv cannot run Python 3.13. Repair the Python installation it references, or rename .venv and rerun setup.ps1. No environment files were removed.'
try {
    & $venvPython -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)'
} catch {
    throw $repairMessage
}
if ($LASTEXITCODE -ne 0) {
    throw $repairMessage
}
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'The pip upgrade failed. Check the error above, then rerun setup.ps1.' }
& $venvPython -m pip install -r (Join-Path $projectRoot 'requirements-dev.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the error above, then rerun setup.ps1.' }
Write-Host 'Python environment ready. Run .\run.ps1 to start Much ADO About Jira.'
