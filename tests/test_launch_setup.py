import asyncio
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from dataclasses import replace
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('powershell.exe') or shutil.which('powershell')


def powershell(script):
    if os.name != 'nt' or not POWERSHELL:
        pytest.skip('Windows PowerShell setup scripts')
    result = subprocess.run(
        [POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def test_windowed_launch_uses_safe_uvicorn_logging(monkeypatch):
    import app as application
    import uvicorn

    monkeypatch.setattr(application, 'acquire_instance', lambda port: True)
    monkeypatch.setattr(application, 'listener_identity', lambda host, port: 'none')
    monkeypatch.setattr(sys, 'argv', ['app.py', '--smoke-test'])
    monkeypatch.setenv('MUCH_ADO_STATE_DIR', 'unused-isolated-test-state')
    with patch.object(sys, 'stdout', None), patch.object(sys, 'stderr', None), patch.object(uvicorn, 'run') as run:
        application.main()
        options = run.call_args.kwargs
        assert options['log_config'] is None
        # Exercise the original formatter failure path, rather than only the call signature.
        uvicorn.Config('dummy:app', **options)


def test_packaged_smoke_with_missing_config_never_imports_onboarding(tmp_path, monkeypatch):
    import builtins
    import config

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == 'onboarding':
            raise AssertionError('Smoke launch must never enter onboarding')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(config, 'user_config_path', lambda: tmp_path / 'missing.toml')
    monkeypatch.setattr(sys, 'argv', ['MuchADOAboutJira.exe', '--smoke-test'])
    with patch.object(builtins, '__import__', guarded_import), pytest.raises(SystemExit) as error:
        runpy.run_path(str(ROOT / 'launcher.py'), run_name='__main__')
    assert 'Smoke-test configuration is missing' in str(error.value)


@pytest.mark.parametrize('enabled_source', [False, True])
def test_smoke_launch_rejects_normal_state_or_enabled_sources(monkeypatch, enabled_source):
    import app as application

    monkeypatch.setattr(sys, 'argv', ['app.py', '--smoke-test'])
    if enabled_source:
        monkeypatch.setenv('MUCH_ADO_STATE_DIR', 'unused-isolated-test-state')
        monkeypatch.setattr(application, 'settings', replace(
            application.settings,
            azure_devops=replace(application.settings.azure_devops, enabled=True),
        ))
    else:
        monkeypatch.delenv('MUCH_ADO_STATE_DIR', raising=False)
    with patch.object(application, 'acquire_instance') as acquire, pytest.raises(ValueError):
        application.main()
    acquire.assert_not_called()


@pytest.mark.asyncio
async def test_smoke_lifespan_skips_startup_registration(monkeypatch):
    import app as application
    async def periodic():
        await asyncio.Event().wait()

    monkeypatch.setattr(sys, 'argv', ['app.py', '--smoke-test'])
    with patch.object(application.startup, 'initialize') as register, patch.object(application.store, 'initialize'), patch.object(application.coordinator, 'run_periodic', periodic), patch.object(application.coordinator, 'stop'):
        async with application.lifespan(application.app):
            register.assert_not_called()


@pytest.mark.parametrize('stage, message', [
    ('version', 'existing .venv cannot run Python 3.13'),
    ('upgrade', 'pip upgrade failed'),
    ('dependencies', 'Dependency installation failed'),
])
def test_setup_rejects_native_failures_without_claiming_success(tmp_path, stage, message):
    shutil.copyfile(ROOT / 'setup.ps1', tmp_path / 'setup.ps1')
    python_path = tmp_path / '.venv' / 'Scripts' / 'python.exe'
    python_path.parent.mkdir(parents=True)
    python_path.touch()
    script = f"""
$ErrorActionPreference = 'Stop'
function Invoke-FakePython {{
    $global:LASTEXITCODE = 0
    if (({ps_quote(stage)} -eq 'version' -and $args[0] -eq '-c') -or
        ({ps_quote(stage)} -eq 'upgrade' -and $args -contains '--upgrade') -or
        ({ps_quote(stage)} -eq 'dependencies' -and $args -contains '-r')) {{ $global:LASTEXITCODE = 7 }}
}}
Set-Alias -Name {ps_quote(python_path)} -Value Invoke-FakePython
try {{ & {ps_quote(tmp_path / 'setup.ps1')}; throw 'Expected setup failure' }}
catch {{ Write-Output $_.Exception.Message }}
"""
    output = powershell(script)
    assert message in output
    assert 'Python environment ready' not in output
    assert python_path.exists()


@pytest.mark.parametrize('source', ['path', 'launcher', 'registry', 'missing'])
def test_setup_selects_verified_python313_before_creating_environment(tmp_path, source):
    shutil.copyfile(ROOT / 'setup.ps1', tmp_path / 'setup.ps1')
    old_python = tmp_path / 'python312.exe'
    python313 = tmp_path / 'python313.exe'
    launcher = tmp_path / 'py.exe'
    for executable in (old_python, python313, launcher):
        executable.touch()
    venv_python = tmp_path / '.venv' / 'Scripts' / 'python.exe'
    script = f"""
$ErrorActionPreference = 'Stop'
function Invoke-Python312 {{
    if ($args -contains 'venv') {{ throw 'Created environment with unsupported Python' }}
    $global:LASTEXITCODE = 1
    Write-Output {ps_quote(old_python)}
}}
function Invoke-Python313 {{
    $global:LASTEXITCODE = 0
    if ($args -contains 'venv') {{
        New-Item -ItemType Directory -Force -Path {ps_quote(venv_python.parent)} | Out-Null
        New-Item -ItemType File -Path {ps_quote(venv_python)} | Out-Null
        Set-Alias -Name {ps_quote(venv_python)} -Value Invoke-Python313 -Scope Global
        Write-Output 'Created environment with Python 3.13'
    }} elseif ($args -contains '-c') {{
        Write-Output {ps_quote(python313)}
    }}
}}
function Invoke-Py {{
    if ($args[0] -ne '-3.13') {{ throw 'Launcher used its unsupported default Python' }}
    Invoke-Python313 @args
}}
Set-Alias -Name {ps_quote(old_python)} -Value Invoke-Python312
Set-Alias -Name {ps_quote(python313)} -Value Invoke-Python313
Set-Alias -Name {ps_quote(launcher)} -Value Invoke-Py
function Get-Command {{
    param($Name, $ErrorAction)
    if ($Name -eq 'python') {{
        $path = {ps_quote(python313)}
        if ({ps_quote(source)} -ne 'path') {{ $path = {ps_quote(old_python)} }}
        return [PSCustomObject]@{{ Source = $path }}
    }}
    if ($Name -eq 'py' -and {ps_quote(source)} -eq 'launcher') {{
        return [PSCustomObject]@{{ Source = {ps_quote(launcher)} }}
    }}
}}
function Test-Path {{
    param($Path, $LiteralPath)
    if ($Path -eq 'HKCU:\\Software\\Python\\PythonCore\\3.13\\InstallPath') {{
        return ({ps_quote(source)} -eq 'registry')
    }}
    if ($LiteralPath) {{ return Microsoft.PowerShell.Management\\Test-Path -LiteralPath $LiteralPath }}
    return Microsoft.PowerShell.Management\\Test-Path -Path $Path
}}
function Get-ItemProperty {{
    param($Path)
    return [PSCustomObject]@{{ ExecutablePath = {ps_quote(python313)} }}
}}
try {{ & {ps_quote(tmp_path / 'setup.ps1')} }}
catch {{ Write-Output $_.Exception.Message }}
"""
    output = powershell(script)
    assert 'Created environment with unsupported Python' not in output
    assert 'Launcher used its unsupported default Python' not in output
    if source == 'missing':
        assert 'Python 3.13 was not found' in output
        assert 'Python environment ready' not in output
        assert not venv_python.exists()
    else:
        assert 'Created environment with Python 3.13' in output
        assert 'Python environment ready' in output
        assert venv_python.exists()


def installer_stub(tool_root, fail_download=False):
    return f"""
$ErrorActionPreference = 'Stop'
function Invoke-BrokenAcli {{ $global:LASTEXITCODE = 9 }}
function Invoke-GoodAcli {{ $global:LASTEXITCODE = 0 }}
Set-Alias -Name {ps_quote(tool_root / 'acli.exe')} -Value Invoke-BrokenAcli
function Invoke-WebRequest {{
    param([string]$Uri, [string]$OutFile)
    [IO.File]::WriteAllText($OutFile, 'good')
    Set-Alias -Name $OutFile -Value Invoke-GoodAcli -Scope Global
    Set-Alias -Name {ps_quote(tool_root / 'acli.exe')} -Value Invoke-GoodAcli -Scope Global
    {'throw "Synthetic interrupted download"' if fail_download else ''}
}}
try {{ & {ps_quote(ROOT / 'setup-connectors.ps1')} -ToolRoot {ps_quote(tool_root)} -JiraOnly }}
catch {{ Write-Output $_.Exception.Message }}
"""


def test_connector_setup_replaces_broken_cli_and_removes_temporary_download(tmp_path):
    (tmp_path / 'acli.exe').write_text('broken')
    output = powershell(installer_stub(tmp_path))
    assert 'Connector tools installed without signing in' in output
    assert (tmp_path / 'acli.exe').read_text() == 'good'
    assert not list(tmp_path.glob('acli-download-*'))


def test_interrupted_connector_download_preserves_existing_tool_and_is_retryable(tmp_path):
    (tmp_path / 'acli.exe').write_text('broken')
    output = powershell(installer_stub(tmp_path, fail_download=True))
    assert 'Synthetic interrupted download' in output
    assert (tmp_path / 'acli.exe').read_text() == 'broken'
    assert not list(tmp_path.glob('acli-download-*'))
    assert 'Connector tools installed without signing in' not in output
    output = powershell(installer_stub(tmp_path))
    assert 'Connector tools installed without signing in' in output
    assert (tmp_path / 'acli.exe').read_text() == 'good'


def test_windows_smoke_script_runs_source_without_external_effects():
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if os.name != 'nt' or not pythonw.exists():
        pytest.skip('Windows background Python executable')
    output = powershell(
        f"& {ps_quote(ROOT / 'scripts' / 'smoke-windows.ps1')} "
        f"-Executable {ps_quote(pythonw)} -SourceEntryPoint {ps_quote(ROOT / 'app.py')}"
    )
    assert 'Windowed launch, offline dashboard, and static assets passed' in output
