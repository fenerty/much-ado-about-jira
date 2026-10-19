from pathlib import Path

import pytest

from config import AppSettings, AzureDevOpsSettings, JiraSettings, Settings, load_settings
from models import ConnectorHealth, ConnectorResult
from refresh import RefreshCoordinator
from store import Store


@pytest.mark.parametrize('name', ['refresh_timeout_seconds', 'history_timeout_seconds'])
@pytest.mark.parametrize('value', [0, -1, True, '10', float('inf'), float('nan')])
def test_ado_history_budgets_require_positive_finite_seconds(name, value):
    with pytest.raises(ValueError, match=f'azure_devops.{name}'):
        AzureDevOpsSettings(**{name: value})


@pytest.mark.parametrize('name', ['history_batch_size', 'history_page_size'])
@pytest.mark.parametrize('value', [0, -1, True, 1.5, '50'])
def test_ado_history_page_and_rotation_limits_require_positive_integers(name, value):
    with pytest.raises(ValueError, match=f'azure_devops.{name}'):
        AzureDevOpsSettings(**{name: value})


def test_existing_configuration_gets_durable_ado_history_defaults(tmp_path):
    path = tmp_path / 'settings.toml'
    path.write_text('[azure_devops]\nenabled = false\n', encoding='utf-8')
    ado = load_settings(path).azure_devops
    assert ado.refresh_timeout_seconds == 60
    assert ado.history_timeout_seconds == 10
    assert ado.history_batch_size == 50
    assert ado.history_page_size == 100
    assert AzureDevOpsSettings(history_timeout_seconds=0.01).history_timeout_seconds == 0.01


def test_example_documents_durable_ado_history_limits():
    ado = load_settings(Path(__file__).resolve().parents[1] / 'settings.example.toml').azure_devops
    assert not ado.enabled
    assert ado.history_page_size == 100


@pytest.mark.asyncio
async def test_coordinator_loads_verified_ado_progress_and_saves_accepted_checkpoint(tmp_path, monkeypatch):
    settings = Settings(AppSettings(activity_retention_days=17),
                        AzureDevOpsSettings(organization='example', expected_account='me@example.test'),
                        JiraSettings(enabled=False))
    store = Store(tmp_path / 'cache.sqlite3', cache_bindings=settings.cache_bindings)
    store.initialize()
    scope = settings.cache_bindings['azure_devops']['scope']
    store.save_connector_state('azure_devops', {'cursor': 'before'})
    coordinator = RefreshCoordinator(settings, store)
    ado, jira = coordinator.connectors
    calls = []

    async def refresh_ado():
        ado.scope_verified(scope)
        calls.append(ado.state_loader(scope))
        assert ado.history_inventory_loader(scope) == []
        return ConnectorResult(connector='azure_devops', cache_scope=scope,
                               checkpoint={'cursor': 'accepted'},
                               health=ConnectorHealth(connector='azure_devops', state='partial',
                                                      message='History is advancing'))

    async def refresh_jira():
        return ConnectorResult(connector='jira', health=ConnectorHealth(
            connector='jira', state='disabled', message='Disabled'))

    monkeypatch.setattr(ado, 'refresh', refresh_ado)
    monkeypatch.setattr(jira, 'refresh', refresh_jira)
    await coordinator.refresh()
    assert calls == [{'cursor': 'before'}]
    assert store.load_connector_state('azure_devops') == {'cursor': 'accepted'}


@pytest.mark.asyncio
async def test_failed_ado_refresh_cannot_advance_saved_progress(tmp_path, monkeypatch):
    settings = Settings(AppSettings(), AzureDevOpsSettings(), JiraSettings(enabled=False))
    store = Store(tmp_path / 'cache.sqlite3')
    store.initialize()
    store.save_connector_state('azure_devops', {'cursor': 'before'})
    coordinator = RefreshCoordinator(settings, store)

    async def failed_ado():
        return ConnectorResult(connector='azure_devops', checkpoint={'cursor': 'unaccepted'},
                               health=ConnectorHealth(connector='azure_devops', state='error',
                                                      message='Read failed'))

    async def disabled_jira():
        return ConnectorResult(connector='jira', health=ConnectorHealth(
            connector='jira', state='disabled', message='Disabled'))

    monkeypatch.setattr(coordinator.connectors[0], 'refresh', failed_ado)
    monkeypatch.setattr(coordinator.connectors[1], 'refresh', disabled_jira)
    await coordinator.refresh()
    assert store.load_connector_state('azure_devops') == {'cursor': 'before'}
