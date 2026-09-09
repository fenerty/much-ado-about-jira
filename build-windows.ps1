param([string]$Python = '.\.venv\Scripts\python.exe')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& $Python -m PyInstaller --noconfirm --onedir --windowed --name MuchADOAboutJira --distpath dist --workpath build --add-data 'static;static' --add-data 'settings.example.toml;.' --add-data 'setup-connectors.ps1;.' --add-data 'sign-in.ps1;.' --add-data 'scripts/cli-bridge.ps1;scripts' launcher.py
if ($LASTEXITCODE) { throw 'Windows application build failed.' }
Copy-Item -LiteralPath 'QUICKSTART.txt' -Destination 'dist\MuchADOAboutJira\READ ME FIRST.txt'
Compress-Archive -Path 'dist\MuchADOAboutJira' -DestinationPath 'dist\MuchADOAboutJira-Windows-x64.zip' -Force
$hash = (Get-FileHash -LiteralPath 'dist\MuchADOAboutJira-Windows-x64.zip' -Algorithm SHA256).Hash.ToLower()
"$hash  MuchADOAboutJira-Windows-x64.zip" | Set-Content -Encoding ascii 'dist\SHA256SUMS.txt'
