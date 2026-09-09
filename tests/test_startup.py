from pathlib import Path
from startup import StartupSettings, RUN_KEY, PREF_KEY, VALUE_NAME


class FakeStartup(StartupSettings):
    supported = True
    def __init__(self):
        super().__init__(Path('example'))
        self.registry = {}
    def command(self):
        return 'pythonw app.py --background'
    def read(self, path, name):
        return self.registry.get((path, name))
    def write(self, path, name, value):
        if value is None:
            self.registry.pop((path, name), None)
        else:
            self.registry[path, name] = value


def test_default_registers_and_opt_out_survives_restart():
    settings = FakeStartup()
    settings.initialize()
    assert settings.status()['enabled']
    settings.set_enabled(False)
    settings.initialize()
    assert not settings.status()['enabled']
    assert settings.read(PREF_KEY, 'LaunchEnabled') == '0'
    settings.set_enabled(True)
    assert settings.status()['enabled']


def test_external_removal_is_respected():
    settings = FakeStartup(); settings.initialize()
    settings.write(RUN_KEY, VALUE_NAME, None)
    settings.initialize()
    assert not settings.status()['enabled']


def test_registration_error_does_not_prevent_app_start():
    settings = FakeStartup()
    def fail(*args):
        raise PermissionError('denied')
    settings.write = fail
    settings.initialize()
    assert settings.status()['error']
    assert not settings.status()['enabled']
