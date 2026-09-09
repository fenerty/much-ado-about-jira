$ErrorActionPreference = 'Stop'
Write-Host 'Sign in to your selected sources. The dashboard only reads work data.'
if ($env:MUCH_ADO_LOGIN_AZURE -eq 'True') {
    $azureCommand = Get-Command az -ErrorAction SilentlyContinue
    $azurePath = if ($azureCommand) { $azureCommand.Source } else { Join-Path $env:MUCH_ADO_TOOL_ROOT 'azure-cli\bin\az.cmd' }
    & $azurePath login --allow-no-subscriptions
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}
if ($env:MUCH_ADO_LOGIN_JIRA -eq 'True') {
    & (Join-Path $env:MUCH_ADO_TOOL_ROOT 'acli.exe') jira auth login --web
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}
