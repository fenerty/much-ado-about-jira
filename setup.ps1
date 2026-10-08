$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

function Find-Python {
    $candidates = @()
    foreach ($name in @('python3.13', 'python', 'py')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            $arguments = @()
            if ($name -eq 'py') { $arguments = @('-3.13') }
            $candidates += @{ Path = $command.Source; Arguments = $arguments }
        }
    }

    $registryPath = 'HKCU:\Software\Python\PythonCore\3.13\InstallPath'
    if (Test-Path $registryPath) {
        $registered = (Get-ItemProperty $registryPath).ExecutablePath
        if ($registered -and (Test-Path -LiteralPath $registered)) {
            $candidates += @{ Path = $registered; Arguments = @() }
        }
    }
    foreach ($candidate in $candidates) {
        try {
            $arguments = $candidate.Arguments
            $resolved = & $candidate.Path @arguments -c 'import sys; print(sys.executable); sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved -is [string] -and (Test-Path -LiteralPath $resolved)) {
                return $resolved
            }
        } catch {
            # An unusable PATH entry must not hide another installed Python 3.13.
        }
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
