"""Per-user Windows startup registration; no administrator access required."""
from pathlib import Path
import os
import subprocess
import sys

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
PREF_KEY = r'Software\MuchADOAboutJira'
VALUE_NAME = 'MuchADOAboutJira'


class StartupSettings:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.error = None

    @property
    def supported(self):
        return os.name == 'nt'

    def command(self):
        if getattr(sys, 'frozen', False):
            return subprocess.list2cmdline([sys.executable, '--background'])
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        if not pythonw.is_file():
            raise OSError('The Windows background Python launcher is missing.')
        return subprocess.list2cmdline([str(pythonw), str(self.project_root / 'app.py'), '--background'])

    def read(self, path, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def write(self, path, name, value):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            if value is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def status(self):
        if not self.supported:
            return {'supported': False, 'enabled': False, 'error': None}
        try:
            return {'supported': True, 'enabled': self.read(RUN_KEY, VALUE_NAME) == self.command(), 'error': self.error}
        except OSError:
            return {'supported': True, 'enabled': False, 'error': 'Windows startup settings could not be read.'}

    def set_enabled(self, enabled):
        self.write(RUN_KEY, VALUE_NAME, self.command() if enabled else None)
        self.write(PREF_KEY, 'LaunchEnabled', '1' if enabled else '0')
        self.error = None
        return self.status()

    def initialize(self):
        if not self.supported:
            return
        try:
            # Register the default on first launch. Respect subsequent opt-outs,
            # including removal through Windows startup settings.
            if self.read(PREF_KEY, 'LaunchEnabled') is None:
                self.set_enabled(True)
        except OSError:
            self.error = 'Could not enable launch at sign-in. Try the setting again.'


def acquire_instance(port=8765):
    """Hold the returned Windows mutex handle for the server's lifetime."""
    if os.name != 'nt':
        return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel.CreateMutexW(None, False, f'Local\\MuchADOAboutJiraServer-{port}')
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
        return None
    return handle
