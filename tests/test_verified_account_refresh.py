import asyncio
import sqlite3
from datetime import timedelta

import pytest

from config import AppSettings, AzureDevOpsSettings, JiraSettings, Settings, source_scope
from connectors.base import ConnectorFailure
from models import ConnectorHealth
from refresh import RefreshCoordinator
from store import Store
from tests.test_cache_scopes import ACCOUNT, SITE, rows, snapshot


OTHER_ACCOUNT = 'other@example.test'


def coordinator_with_cache(tmp_path, connector):
    realm = SITE if connector == 'jira' else 'org-one'
    settings = Settings(
        AppSettings(),
        AzureDevOpsSettings(enabled=connector == 'azure_devops', organization='org-one'),
        JiraSettings(enabled=connector == 'jira', site=SITE),
    )
    store = Store(tmp_path / 'cache.db', settings.cache_bindings)
    store.initialize()
    store.replace_connector(snapshot(connector, realm), 60)
    store.save_connector_state(connector, {'cursor': 'account-a'})
    event_id = store.load()[1][0].id
    store.mark_batch([event_id], 'unread')
    coordinator = RefreshCoordinator(settings, store)
    selected = next(entry for entry in coordinator.connectors if entry.name == connector)
    return coordinator, selected, realm, event_id


def stub_verified_read(monkeypatch, connector, realm, account, read):
    monkeypatch.setattr(connector, '_find_cli', lambda: 'synthetic-cli')
    if connector.name == 'jira':
        async def authenticate(executable):
            return {'email': account, 'site': realm, 'account_id': account}
        monkeypatch.setattr(connector, '_authenticate', authenticate)
        monkeypatch.setattr(connector, '_query_candidates', read)
    else:
        monkeypatch.setattr(connector, '_authenticate', lambda executable: account)
        monkeypatch.setattr(connector, '_access_token', lambda executable: 'synthetic-token')
        monkeypatch.setattr(connector, '_connection_identity', read)


@pytest.mark.asyncio
@pytest.mark.parametrize('connector_name', ['jira', 'azure_devops'])
@pytest.mark.parametrize('failure', [ValueError, RuntimeError, TimeoutError])
async def test_verified_account_switch_hides_previous_cache_before_read_finishes(
    tmp_path, monkeypatch, connector_name, failure
):
    coordinator, connector, realm, old_event = coordinator_with_cache(tmp_path, connector_name)
    store = coordinator.store
    previous = {table: rows(store, table) for table in ('entities', 'local_state', 'app_meta', 'connector_runs')}
    entered, finish = asyncio.Event(), asyncio.Event()

    async def blocked_read(*args, **kwargs):
        entered.set()
        await finish.wait()
        raise failure('synthetic read failure')

    stub_verified_read(monkeypatch, connector, realm, OTHER_ACCOUNT, blocked_read)
    coordinator._persistence_errors[connector_name] = ConnectorHealth(
        connector=connector_name, state='error', message='Account A failure', error_code='OLD_ERROR'
    )
    task = asyncio.create_task(coordinator.refresh())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert store.load() == ([], [], [])
        assert store.load_dismissed() == []
        assert store.load_connector_state(connector_name) == {}
        assert coordinator.dashboard()['tracked_items'] == []
        assert connector_name not in coordinator._persistence_errors
        assert not store.set_local_state(old_event, 'seen')
    finally:
        finish.set()
        await task

    work, activities, health = store.load()
    current_health = next(entry for entry in health if entry.connector == connector_name)
    assert not work and not activities
    assert current_health.state == 'error'
    assert current_health.last_success_at is None
    assert store.load_connector_state(connector_name) == {}
    for table in ('entities', 'local_state', 'app_meta'):
        assert rows(store, table) == previous[table]
    for old_health in previous['connector_runs']:
        assert old_health in rows(store, 'connector_runs')
    store.activate_connector_scope(connector_name, source_scope(connector_name, realm, ACCOUNT))
    assert store.load()[1][0].id == old_event
    assert store.load()[1][0].unread
    assert store.load_connector_state(connector_name) == {'cursor': 'account-a'}


@pytest.mark.asyncio
@pytest.mark.parametrize('connector_name', ['jira', 'azure_devops'])
async def test_post_auth_failure_uses_returning_accounts_snapshot_and_success_time(
    tmp_path, monkeypatch, connector_name
):
    coordinator, connector, realm, old_event = coordinator_with_cache(tmp_path, connector_name)
    store = coordinator.store
    returning = snapshot(connector_name, realm, OTHER_ACCOUNT, title='Account B retained work')
    returning.health.last_success_at -= timedelta(minutes=10)
    store.replace_connector(returning, 60)
    store.save_connector_state(connector_name, {'cursor': 'account-b'})
    store.activate_connector_scope(connector_name, source_scope(connector_name, realm, ACCOUNT))

    async def failed_read(*args, **kwargs):
        raise ConnectorFailure('READ_FAILED', 'Synthetic read failure')

    stub_verified_read(monkeypatch, connector, realm, OTHER_ACCOUNT, failed_read)
    await coordinator.refresh()
    work, activities, health = store.load()
    current_health = next(entry for entry in health if entry.connector == connector_name)
    assert [entry.title for entry in work] == ['Account B retained work']
    assert activities[0].id != old_event
    assert current_health.error_code == 'READ_FAILED'
    assert current_health.last_success_at == returning.health.last_success_at
    assert store.load_connector_state(connector_name) == {'cursor': 'account-b'}


@pytest.mark.asyncio
@pytest.mark.parametrize('connector_name', ['jira', 'azure_devops'])
async def test_verified_switch_remains_isolated_when_health_write_fails(
    tmp_path, monkeypatch, connector_name
):
    coordinator, connector, realm, _ = coordinator_with_cache(tmp_path, connector_name)

    async def failed_read(*args, **kwargs):
        raise ValueError('synthetic read failure')

    stub_verified_read(monkeypatch, connector, realm, OTHER_ACCOUNT, failed_read)
    def failed_health_write(*args):
        raise sqlite3.OperationalError('synthetic database write failure')

    monkeypatch.setattr(coordinator.store, '_write_health', failed_health_write)
    with pytest.raises(sqlite3.OperationalError):
        await coordinator.refresh()
    dashboard = coordinator.dashboard()
    assert dashboard['tracked_items'] == []
    assert dashboard['dismissed'] == []
    assert dashboard['health'][connector_name]['error_code'] == 'CACHE_WRITE_FAILED'
    assert dashboard['health'][connector_name]['last_success_at'] is None


@pytest.mark.asyncio
async def test_failure_before_refresh_does_not_reuse_previous_attempts_verified_scope(tmp_path, monkeypatch):
    coordinator, connector, _, old_event = coordinator_with_cache(tmp_path, 'jira')
    connector.verified_cache_scope = source_scope('jira', SITE, OTHER_ACCOUNT)

    def failed_state_read(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic database read failure')

    monkeypatch.setattr(coordinator.store, 'load_connector_state', failed_state_read)
    result = await coordinator._run_connector(connector)
    assert result.cache_scope is None
    assert coordinator.store.load()[1][0].id == old_event


@pytest.mark.asyncio
async def test_token_failure_after_azure_account_verification_cannot_show_previous_cache(tmp_path, monkeypatch):
    coordinator, connector, realm, _ = coordinator_with_cache(tmp_path, 'azure_devops')

    async def unexpected_read(*args, **kwargs):
        raise AssertionError('No API request can run without a token')

    stub_verified_read(monkeypatch, connector, realm, OTHER_ACCOUNT, unexpected_read)

    def token_failure(executable):
        assert coordinator.store.load() == ([], [], [])
        raise ConnectorFailure('AZ_TOKEN_UNAVAILABLE', 'Synthetic token failure', auth_required=True)

    monkeypatch.setattr(connector, '_access_token', token_failure)
    await coordinator.refresh()
    dashboard = coordinator.dashboard()
    assert dashboard['tracked_items'] == []
    assert dashboard['health']['azure_devops']['state'] == 'auth_required'
    assert dashboard['health']['azure_devops']['last_success_at'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('connector_name', ['jira', 'azure_devops'])
async def test_cancelled_post_auth_refresh_keeps_new_scope_without_exposing_old_cache(
    tmp_path, monkeypatch, connector_name
):
    coordinator, connector, realm, _ = coordinator_with_cache(tmp_path, connector_name)
    entered = asyncio.Event()

    async def pending_read(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    stub_verified_read(monkeypatch, connector, realm, OTHER_ACCOUNT, pending_read)
    task = asyncio.create_task(coordinator.refresh())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert coordinator.store.load() == ([], [], [])
    assert coordinator.dashboard()['tracked_items'] == []
