param(
    [string]$Executable = (Join-Path $PSScriptRoot '..\dist\MuchADOAboutJira\MuchADOAboutJira.exe'),
    # Optional source entry point allows offline testing with pythonw.exe before packaging.
    [string]$SourceEntryPoint,
    [ValidateRange(1, 120)][int]$TimeoutSeconds = 30
)
$ErrorActionPreference = 'Stop'
$executablePath = (Resolve-Path -LiteralPath $Executable).Path
$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$runRoot = Join-Path $temporaryRoot ('much-ado-smoke-' + [guid]::NewGuid().ToString('N'))
$process = $null
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
try {
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
} finally {
    $listener.Stop()
}
try {
    New-Item -ItemType Directory -Path $runRoot | Out-Null
    $configuration = Join-Path $runRoot 'settings.toml'
    @"
[app]
host = "127.0.0.1"
port = $port
[azure_devops]
enabled = false
[jira]
enabled = false
"@ | Set-Content -LiteralPath $configuration -Encoding ascii
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $executablePath
    $startInfo.Arguments = if ($SourceEntryPoint) {
        '"' + (Resolve-Path -LiteralPath $SourceEntryPoint).Path + '" --smoke-test'
    } else { '--smoke-test' }
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.EnvironmentVariables['MUCH_ADO_CONFIG'] = $configuration
    $startInfo.EnvironmentVariables['MUCH_ADO_STATE_DIR'] = $runRoot
    $process = [Diagnostics.Process]::Start($startInfo)
    $baseUrl = "http://127.0.0.1:$port"
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $ready = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($process.HasExited) { throw "The packaged app exited before becoming ready (exit $($process.ExitCode))." }
        try {
            $identity = (Invoke-WebRequest -Uri "$baseUrl/api/identity" -UseBasicParsing -TimeoutSec 2).Content | ConvertFrom-Json
            if ($identity.application -eq 'much-ado-about-jira') { $ready = $true; break }
        } catch {
            Start-Sleep -Milliseconds 100
        }
    }
    if (-not $ready) { throw 'The packaged app did not become ready before the smoke-test deadline.' }
    $dashboard = (Invoke-WebRequest -Uri "$baseUrl/api/dashboard" -UseBasicParsing -TimeoutSec 2).Content | ConvertFrom-Json
    $null = Invoke-WebRequest -Uri $baseUrl -UseBasicParsing -TimeoutSec 2
    $null = Invoke-WebRequest -Uri "$baseUrl/static/app.js" -UseBasicParsing -TimeoutSec 2
    if ($process.HasExited) { throw 'The packaged app exited during the smoke test.' }
    if ($dashboard.health.azure_devops.state -ne 'disabled' -or $dashboard.health.jira.state -ne 'disabled') {
        throw 'Smoke-test sources must remain disabled.'
    }
    Write-Host 'Windowed launch, offline dashboard, and static assets passed.'
} finally {
    if ($process) {
        try {
            if (-not $process.HasExited) {
                try { $process.Kill() } catch { if (-not $process.HasExited) { throw } }
                if (-not $process.WaitForExit(5000)) { throw 'The smoke-test process did not exit during cleanup.' }
            }
        } finally { $process.Dispose() }
    }
    # Verify the exact temporary directory before its recursive removal.
    $resolvedRunRoot = [IO.Path]::GetFullPath($runRoot)
    $allowedPrefix = Join-Path $temporaryRoot 'much-ado-smoke-'
    if (-not $resolvedRunRoot.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing smoke-test cleanup outside its temporary directory.'
    }
    if (Test-Path -LiteralPath $resolvedRunRoot) { Remove-Item -LiteralPath $resolvedRunRoot -Recurse -Force }
}
