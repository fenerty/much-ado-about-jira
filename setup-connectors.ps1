param(
    [string]$ToolRoot,
    [switch]$JiraOnly,
    [switch]$AzureOnly
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectToolRoot = if ($ToolRoot) { $ToolRoot } else { Join-Path $projectRoot '.tools' }
$acliPath = Join-Path $projectToolRoot 'acli.exe'
$azureRoot = Join-Path $projectToolRoot 'azure-cli'
$azureCommand = Join-Path $azureRoot 'bin\az.cmd'

New-Item -ItemType Directory -Path $projectToolRoot -Force | Out-Null

function Test-AtlassianCli([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        & $Path --version | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

if (-not $AzureOnly -and -not (Test-AtlassianCli $acliPath)) {
    Write-Host 'Downloading the official Atlassian CLI...'
    $acliDownload = Join-Path $projectToolRoot ('acli-download-' + [guid]::NewGuid().ToString('N') + '.exe')
    try {
        Invoke-WebRequest `
            -Uri 'https://acli.atlassian.com/windows/latest/acli_windows_amd64/acli.exe' `
            -OutFile $acliDownload
        if (-not (Test-AtlassianCli $acliDownload)) { throw 'The downloaded Atlassian CLI could not start. Retry setup.' }
        Move-Item -LiteralPath $acliDownload -Destination $acliPath -Force
    } finally {
        # Only remove this attempt's temporary file; preserve an existing tool.
        if (Test-Path -LiteralPath $acliDownload) { Remove-Item -LiteralPath $acliDownload -Force }
    }
}

$installedAzure = Get-Command az -ErrorAction SilentlyContinue
if ($installedAzure) {
    $azureCommand = $installedAzure.Source
} elseif (-not $JiraOnly -and -not (Test-Path -LiteralPath $azureCommand)) {
    Write-Host 'Downloading the official Azure CLI 2.90.0 ZIP...'
    $azureZip = Join-Path $env:TEMP 'much-ado-azure-cli-2.90.0-x64.zip'
    Invoke-WebRequest `
        -Uri 'https://azcliprod.blob.core.windows.net/zip/azure-cli-2.90.0-x64.zip' `
        -OutFile $azureZip
    Expand-Archive -LiteralPath $azureZip -DestinationPath $azureRoot -Force
    Remove-Item -LiteralPath $azureZip -Force
}

if (-not $AzureOnly) { & $acliPath --version; if ($LASTEXITCODE) { throw 'Atlassian CLI could not start.' } }
if (-not $JiraOnly) { & $azureCommand version --output json; if ($LASTEXITCODE) { throw 'Azure CLI could not start.' } }

Write-Host ''
Write-Host 'Connector tools installed without signing in.'
Write-Host "Azure sign-in:    `"$azureCommand`" login"
Write-Host "Jira OAuth login: `"$acliPath`" jira auth login --web"
