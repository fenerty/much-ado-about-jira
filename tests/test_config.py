from pathlib import Path
import pytest
import config
from config import load_settings


def test_example_configuration_is_offline_by_default():
    settings = load_settings(Path(__file__).resolve().parents[1] / 'settings.example.toml')
    assert not settings.azure_devops.enabled
    assert not settings.jira.enabled
    assert settings.app.host == '127.0.0.1'


def test_missing_local_config_uses_example_but_explicit_missing_config_fails(tmp_path, monkeypatch):
    example = Path(__file__).resolve().parents[1] / 'settings.example.toml'
    (tmp_path / 'settings.example.toml').write_text(example.read_text())
    monkeypatch.setattr(config, 'PROJECT_ROOT', tmp_path)
    monkeypatch.delenv('MUCH_ADO_CONFIG', raising=False)
    assert not load_settings().jira.enabled
    monkeypatch.setenv('MUCH_ADO_CONFIG', str(tmp_path / 'missing.toml'))
    with pytest.raises(FileNotFoundError):
        load_settings()
