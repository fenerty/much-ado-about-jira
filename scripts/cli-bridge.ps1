param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('az', 'acli')]
    [string]$Tool,

    [Parameter(Mandatory = $true)]
    [string]$ArgumentsJson
)

$ErrorActionPreference = 'Stop'
$toolRoot = Join-Path $env:LOCALAPPDATA 'MuchADOAboutJira\tools'
$executable = if ($Tool -eq 'az') {
    Join-Path $toolRoot 'azure-cli\bin\az.cmd'
} else {
    Join-Path $toolRoot 'acli.exe'
}

if (-not (Test-Path -LiteralPath $executable)) {
    Write-Error "$Tool is not installed in the Much ADO About Jira tools directory."
    exit 127
}

try {
    [string[]]$ToolArguments = @(ConvertFrom-Json -InputObject $ArgumentsJson)
} catch {
    Write-Error 'The CLI bridge received invalid JSON arguments.'
    exit 2
}

& $executable @ToolArguments
exit $LASTEXITCODE
