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
}

& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $projectRoot 'requirements-dev.txt')
Write-Host 'Python environment ready. Run .\run.ps1 to start Much ADO About Jira.'
