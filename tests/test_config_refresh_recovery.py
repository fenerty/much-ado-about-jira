import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from config import AppSettings, AzureDevOpsSettings, JiraSettings, Settings, source_scope
from models import ConnectorHealth, ConnectorResult, utc_now
from refresh import RefreshCoordinator
from store import Store
from tests.test_store import make_item, result


@pytest.mark.parametrize('value', [0, -1, True, '180', float('inf'), float('nan')])
@pytest.mark.parametrize('name', ['refresh_seconds', 'connector_timeout_seconds'])
def test_app_rejects_unbounded_or_nonpositive_polling(name, value):
    with pytest.raises(ValueError, match=f'app.{name}'):
        AppSettings(**{name: value})


@pytest.mark.parametrize('name,value', [('port', 0), ('port', 65536), ('port', True),
                                       ('activity_retention_days', 0), ('stale_after_days', -1)])
def test_app_rejects_invalid_ports_and_windows(name, value):
    with pytest.raises(ValueError, match=f'app.{name}'):
        AppSettings(**{name: value})


def test_connector_limits_reject_invalid_counts_and_preserve_fractional_timeouts():
    with pytest.raises(ValueError, match='azure_devops.max_work_items'):
        AzureDevOpsSettings(max_work_items=0)
    with pytest.raises(ValueError, match='azure_devops.mention_reply_days'):
        AzureDevOpsSettings(mention_reply_days=-1)
    with pytest.raises(ValueError, match='jira.history_batch_size'):
        JiraSettings(history_batch_size=True)
    with pytest.raises(ValueError, match='jira.history_timeout_seconds'):
        JiraSettings(history_timeout_seconds=float('inf'))
    assert JiraSettings(history_timeout_seconds=0.01).history_timeout_seconds == 0.01
    assert AppSettings(refresh_seconds=0.01).refresh_seconds == 0.01
    assert AzureDevOpsSettings(max_comment_candidates=0).max_comment_candidates == 0


def test_scopes_canonicalize_supported_realms_but_keep_accounts_and_sites_separate():
    scope = source_scope('jira', 'https://Example.atlassian.net/', ' ME@example.test ')
    assert scope == source_scope('jira', 'example.atlassian.net', 'me@example.test')
    assert scope != source_scope('jira', 'other.atlassian.net', 'me@example.test')
    assert scope != source_scope('jira', 'example.atlassian.net', 'other@example.test')
    assert source_scope('azure_devops', 'https://dev.azure.com/Team/', 'me@example.test') == source_scope('azure_devops', 'team', 'me@example.test')
    settings = Settings(AppSettings(), AzureDevOpsSettings(), JiraSettings(site='https://example.atlassian.net'))
    assert settings.cache_bindings['jira']['scope'].startswith('unverified:')
    assert settings.cache_bindings['jira']['legacy_history_scope'] is None


@pytest.mark.asyncio
async def test_periodic_refresh_retries_after_storage_failure_and_still_observes_stop(tmp_path):
    coordinator = RefreshCoordinator(Settings(AppSettings(refresh_seconds=0.001), AzureDevOpsSettings(), JiraSettings()), Store(tmp_path / 'cache.db'))
    attempts = []

    async def flaky_refresh():
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError('synthetic locked database')
        coordinator.stop()

    coordinator.refresh = flaky_refresh
    await asyncio.wait_for(coordinator.run_periodic(), timeout=1)
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_periodic_refresh_does_not_swallow_cancellation(tmp_path):
    coordinator = RefreshCoordinator(Settings(AppSettings(), AzureDevOpsSettings(), JiraSettings()), Store(tmp_path / 'cache.db'))
    entered = asyncio.Event()

    async def pending_refresh():
        entered.set()
        await asyncio.Event().wait()

    coordinator.refresh = pending_refresh
    task = asyncio.create_task(coordinator.run_periodic())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_failed_cache_write_remains_visible_without_blocking_the_other_source(tmp_path, monkeypatch):
    store = Store(tmp_path / 'cache.db')
    store.initialize()
    succeeded = utc_now() - timedelta(minutes=5)
    store.record_health(ConnectorHealth(connector='azure_devops', state='ok', message='Previous success', last_success_at=succeeded))
    coordinator = RefreshCoordinator(Settings(AppSettings(), AzureDevOpsSettings(), JiraSettings()), store)
    ado, jira = coordinator.connectors

    async def ado_refresh():
        return ConnectorResult(connector='azure_devops', health=ConnectorHealth(connector='azure_devops', state='ok', message='Fresh', last_success_at=utc_now()))

    async def jira_refresh():
        return result(make_item())

    monkeypatch.setattr(ado, 'refresh', ado_refresh)
    monkeypatch.setattr(jira, 'refresh', jira_refresh)
    monkeypatch.setattr(jira, 'checkpoint', lambda: {'cursor': 'saved'})
    original_replace = store.replace_connector

    def failed_ado_write(snapshot, retention):
        if snapshot.connector == 'azure_devops':
            raise sqlite3.OperationalError('synthetic locked database')
        original_replace(snapshot, retention)

    monkeypatch.setattr(store, 'replace_connector', failed_ado_write)
    with pytest.raises(sqlite3.OperationalError):
        await coordinator.refresh()
    dashboard = coordinator.dashboard()
    assert dashboard['health']['azure_devops']['error_code'] == 'CACHE_WRITE_FAILED'
    assert datetime.fromisoformat(dashboard['health']['azure_devops']['last_success_at']) == succeeded
    assert len(dashboard['tracked_items']) == 1
    assert store.load_connector_state('jira') == {'cursor': 'saved'}

    monkeypatch.setattr(store, 'replace_connector', original_replace)
    await coordinator.refresh()
    assert coordinator.dashboard()['health']['azure_devops']['state'] == 'ok'
    assert coordinator._persistence_errors == {}
