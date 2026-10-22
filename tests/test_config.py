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


def test_existing_config_receives_safe_history_defaults(tmp_path):
    path = tmp_path / 'settings.toml'
    path.write_text('[app]\n[azure_devops]\n[jira]\n', encoding='utf-8')
    settings = load_settings(path)
    assert settings.azure_devops.history_request_timeout_seconds == 5
    assert settings.jira.changelog_enabled is False
    assert settings.jira.changelog_batch_size == 8
    assert settings.jira.changelog_page_size == 100


@pytest.mark.parametrize('value', [0, -1, True, '5', float('inf'), float('nan')])
def test_history_request_timeout_must_be_bounded_positive(value):
    with pytest.raises(ValueError, match='azure_devops.history_request_timeout_seconds'):
        config.AzureDevOpsSettings(history_request_timeout_seconds=value)


@pytest.mark.parametrize('settings', [
    {'changelog_enabled': 'yes'}, {'changelog_enabled': 1},
    {'changelog_batch_size': 0}, {'changelog_batch_size': True},
    {'changelog_page_size': 0}, {'changelog_page_size': 101},
    {'changelog_timeout_seconds': 0}, {'changelog_timeout_seconds': float('inf')},
    {'changelog_request_timeout_seconds': 0}, {'changelog_request_timeout_seconds': True},
])
def test_changelog_settings_reject_unbounded_or_ambiguous_values(settings):
    with pytest.raises(ValueError, match='jira.changelog_'):
        config.JiraSettings(**settings)
