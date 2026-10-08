from datetime import timedelta

import pytest

from models import ConnectorHealth
from store import Store
from tests.test_activity_read_state import use_legacy_version
from tests.test_cache_scopes import ACCOUNT, NOW, SITE, binding, rows, scoped, snapshot


@pytest.fixture
def advance(monkeypatch):
    clock = {'at': NOW + timedelta(days=1)}
    monkeypatch.setattr('store.utc_now', lambda: clock['at'])

    def tick():
        clock['at'] += timedelta(minutes=1)
        return clock['at']

    return tick


def mature_legacy(path, advance, *, state='read', full_hash=False):
    legacy = Store(path)
    legacy.initialize()
    original = snapshot()
    advance()
    legacy.replace_connector(original, 60)
    advance()
    legacy.replace_connector(snapshot(), 60)
    if full_hash:
        use_legacy_version(legacy, original.activities[0])
    advance()
    if state.endswith('unread'):
        legacy.set_local_state(original.activities[0].id, 'unread')
    else:
        legacy.set_local_state(original.activities[0].id, 'seen')
    if state.startswith('hidden'):
        legacy.dismiss_updates(original.activities[0].id)
    legacy.save_connector_state('jira', {'scope': binding()['legacy_history_scope']})
    return legacy, original


def current_store(path, advance, *, result=None):
    store = scoped(path, {'jira': binding()})
    advance()
    store.replace_connector(result or snapshot(), 60)
    return store


def qualified_id(raw_id, chosen=None):
    chosen = chosen or binding()
    return f"jira@{chosen['scope']}:{raw_id}"


def event_state(store, raw_id):
    with store._connect() as connection:
        row = connection.execute(
            'SELECT * FROM local_state WHERE entity_id = ?', (qualified_id(raw_id),)
        ).fetchone()
    return dict(row) if row else None


def legacy_rows(store):
    with store._connect() as connection:
        return {
            'entities': [dict(row) for row in connection.execute(
                "SELECT * FROM entities WHERE connector = 'jira' ORDER BY entity_id")],
            'local_state': [dict(row) for row in connection.execute(
                "SELECT * FROM local_state WHERE entity_id NOT LIKE 'jira@%' ORDER BY entity_id")],
            'health': [dict(row) for row in connection.execute(
                "SELECT * FROM connector_runs WHERE connector = 'jira'")],
            'meta': [dict(row) for row in connection.execute(
                "SELECT * FROM app_meta WHERE key IN ('baseline:jira', 'connector_state:jira') ORDER BY key")],
        }


@pytest.mark.parametrize('full_hash', [False, True])
@pytest.mark.parametrize('state', ['read', 'unread', 'hidden_read', 'hidden_unread'])
def test_explicit_binding_preserves_same_update_state_and_leaves_legacy_untouched(
    tmp_path, advance, full_hash, state
):
    path = tmp_path / 'cache.db'
    legacy, original = mature_legacy(path, advance, state=state, full_hash=full_hash)
    preserved = legacy_rows(legacy)
    current = snapshot(reasons=['watching'])
    store = current_store(path, advance, result=current)
    before = {table: rows(store, table) for table in ('entities', 'local_state', 'connector_runs', 'app_meta')}

    advance()
    preview = store.bind_legacy_read_state('jira', binding()['scope'])
    assert {table: rows(store, table) for table in before} == before
    applied = store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    assert applied == preview
    assert applied['unread' if state.endswith('unread') else 'read'] == 1
    assert applied['hidden'] == int(state.startswith('hidden'))
    assert legacy_rows(store) == preserved
    if state.startswith('hidden'):
        hidden = [row for row in store.load_dismissed() if row['entity_kind'] == 'activity']
        assert len(hidden) == 1
        assert hidden[0]['unread'] == state.endswith('unread')
        assert store.load()[1] == []
    else:
        assert len(store.load()[1]) == 1
        assert store.load()[1][0].unread == state.endswith('unread')
    with store._connect() as connection:
        assert connection.execute(
            "SELECT value FROM app_meta WHERE key = 'legacy_read_scope:jira'"
        ).fetchone()['value'] == f"jira@{binding()['scope']}"

    saved = {table: rows(store, table) for table in before}
    advance()
    store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    assert {table: rows(store, table) for table in saved} == saved


def test_late_historical_discovery_inherits_read_then_changed_and_new_events_are_unread(tmp_path, advance):
    path = tmp_path / 'cache.db'
    legacy, original = mature_legacy(path, advance, full_hash=True)
    preserved = legacy_rows(legacy)
    empty = snapshot().model_copy(update={'work_items': [], 'activities': []})
    store = current_store(path, advance, result=empty)
    advance()
    store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    advance()
    store.replace_connector(snapshot(), 60)
    assert not store.load()[1][0].unread
    assert event_state(store, original.activities[0].id) is not None

    store = scoped(path, {'jira': binding()})
    changed = snapshot()
    changed.activities[0].summary = 'The source comment was edited'
    advance()
    store.replace_connector(changed, 60)
    assert store.load()[1][0].unread

    new_event = original.activities[0].model_copy(update={
        'id': original.activities[0].id + ':later',
        'timestamp': NOW + timedelta(minutes=1),
        'summary': 'A later update',
    })
    changed.activities.append(new_event)
    advance()
    store.replace_connector(changed, 60)
    assert all(event.unread for event in store.load()[1])
    assert legacy_rows(store) == preserved


@pytest.mark.parametrize('state', ['read', 'unread', 'hidden_read', 'hidden_unread'])
def test_binding_before_first_snapshot_does_not_overwrite_imported_unread_or_hide(tmp_path, advance, state):
    path = tmp_path / 'cache.db'
    _, original = mature_legacy(path, advance, state=state)
    store = scoped(path, {'jira': binding()})
    health = snapshot().health
    store.record_health(health, cache_scope=binding()['scope'])
    advance()
    store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    advance()
    store.replace_connector(snapshot(), 60)
    saved = event_state(store, original.activities[0].id)
    assert saved is not None
    if state.startswith('hidden'):
        hidden = [row for row in store.load_dismissed() if row['entity_kind'] == 'activity']
        assert len(hidden) == 1 and hidden[0]['unread'] == state.endswith('unread')
    else:
        assert store.load()[1][0].unread == state.endswith('unread')


@pytest.mark.parametrize('action', ['seen', 'unread'])
def test_destination_user_actions_override_legacy_and_survive_rebinding_and_refresh(tmp_path, advance, action):
    path = tmp_path / 'cache.db'
    _, original = mature_legacy(path, advance, state='unread' if action == 'seen' else 'read')
    store = current_store(path, advance)
    advance()
    store.set_local_state(qualified_id(original.activities[0].id), action)
    saved = event_state(store, original.activities[0].id)
    advance()
    counts = store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    assert counts['preserved'] == 1
    assert event_state(store, original.activities[0].id) == saved
    advance()
    store.replace_connector(snapshot(reasons=['watching']), 60)
    store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    assert event_state(store, original.activities[0].id) == saved
    assert store.load()[1][0].unread == (action == 'unread')


def test_destination_hidden_unread_action_is_not_overwritten(tmp_path, advance):
    path = tmp_path / 'cache.db'
    _, original = mature_legacy(path, advance)
    store = current_store(path, advance)
    event_id = qualified_id(original.activities[0].id)
    advance()
    store.set_local_state(event_id, 'unread')
    store.dismiss_updates(event_id)
    saved = event_state(store, original.activities[0].id)
    advance()
    store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    assert event_state(store, original.activities[0].id) == saved
    hidden = [row for row in store.load_dismissed() if row['entity_kind'] == 'activity']
    assert len(hidden) == 1 and hidden[0]['unread']


def test_first_bound_snapshot_only_inherits_read_for_the_exact_acknowledged_event(tmp_path, advance):
    path = tmp_path / 'cache.db'
    _, original = mature_legacy(path, advance)
    store = scoped(path, {'jira': binding()})
    store.record_health(snapshot().health, cache_scope=binding()['scope'])
    advance()
    store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    fresh = snapshot()
    fresh.activities.append(original.activities[0].model_copy(update={
        'id': original.activities[0].id + ':unseen', 'summary': 'New material update',
    }))
    advance()
    store.replace_connector(fresh, 60)
    events = {event.id: event for event in store.load()[1]}
    assert not events[qualified_id(original.activities[0].id)].unread
    assert events[qualified_id(original.activities[0].id + ':unseen')].unread


def test_changed_current_content_and_new_baseline_event_are_not_marked_read_by_binding(tmp_path, advance):
    path = tmp_path / 'cache.db'
    _, original = mature_legacy(path, advance)
    current = snapshot()
    current.activities[0].summary = 'A source edit after the previous read'
    current.activities.append(original.activities[0].model_copy(update={
        'id': original.activities[0].id + ':new', 'summary': 'A previously unseen event',
    }))
    store = current_store(path, advance, result=current)
    assert all(not event.unread for event in store.load()[1])
    advance()
    counts = store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    assert counts['unread'] == 2
    assert all(event.unread for event in store.load()[1])


def test_legacy_stale_seen_version_is_not_reinterpreted_as_read(tmp_path, advance):
    path = tmp_path / 'cache.db'
    legacy, original = mature_legacy(path, advance, full_hash=True)
    advance()
    changed = snapshot()
    changed.activities[0].summary = 'Legacy source content changed after reading'
    legacy.replace_connector(changed, 60)
    assert legacy.load()[1][0].unread
    store = current_store(path, advance, result=changed)
    advance()
    store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    assert store.load()[1][0].unread


@pytest.mark.parametrize('realm,account', [('two.atlassian.net', ACCOUNT), (SITE, 'other@example.test')])
def test_legacy_binding_cannot_rebind_same_keys_to_other_source_or_account(tmp_path, advance, realm, account):
    path = tmp_path / 'cache.db'
    mature_legacy(path, advance)
    first = current_store(path, advance)
    advance()
    first.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    chosen = binding(realm=realm, account=account)
    other = scoped(path, {'jira': chosen})
    advance()
    empty = snapshot(realm=realm, account=account).model_copy(update={'work_items': [], 'activities': []})
    other.replace_connector(empty, 60)
    advance()
    other.replace_connector(snapshot(realm=realm, account=account), 60)
    assert other.load()[1][0].unread
    saved = {table: rows(other, table) for table in ('entities', 'local_state', 'connector_runs', 'app_meta')}
    with pytest.raises(ValueError):
        other.bind_legacy_read_state('jira', chosen['scope'], apply=True)
    assert {table: rows(other, table) for table in saved} == saved


def test_binding_requires_configured_account_matching_scope_and_successful_scope_health(tmp_path, advance):
    path = tmp_path / 'cache.db'
    mature_legacy(path, advance)
    store = scoped(path, {'jira': binding()})
    with pytest.raises(ValueError):
        store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    store.record_health(ConnectorHealth(connector='jira', state='error', message='Not verified'),
                        cache_scope=binding()['scope'])
    with pytest.raises(ValueError):
        store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    store.record_health(snapshot().health, cache_scope=binding()['scope'])
    with pytest.raises(ValueError):
        store.bind_legacy_read_state('jira', binding(account='other@example.test')['scope'], apply=True)
    unknown = scoped(path, {'jira': binding(account='')})
    with pytest.raises(ValueError):
        unknown.bind_legacy_read_state('jira', binding()['scope'], apply=True)


def test_already_adopted_first_snapshot_rejects_binding_and_preserves_read_cache(tmp_path, advance):
    path = tmp_path / 'cache.db'
    legacy = Store(path)
    legacy.initialize()
    advance()
    legacy.replace_connector(snapshot(), 60)
    legacy.save_connector_state('jira', {'scope': binding()['legacy_history_scope']})
    store = scoped(path, {'jira': binding()})
    assert not store.load()[1][0].unread
    assert legacy_rows(store)['entities'] == []
    saved = {table: rows(store, table) for table in ('entities', 'local_state', 'connector_runs', 'app_meta')}
    with pytest.raises(ValueError, match='No preserved legacy updates'):
        store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    assert {table: rows(store, table) for table in saved} == saved
    assert not store.load()[1][0].unread


def test_failed_second_state_lookup_rolls_back_first_write_and_binding(tmp_path, advance, monkeypatch):
    path = tmp_path / 'cache.db'
    legacy, original = mature_legacy(path, advance)
    two_events = snapshot()
    two_events.activities.append(original.activities[0].model_copy(update={
        'id': original.activities[0].id + ':second', 'summary': 'Another unchanged update',
    }))
    advance()
    legacy.replace_connector(two_events, 60)
    legacy.mark_batch([event.id for event in two_events.activities], 'seen')
    store = current_store(path, advance, result=two_events)
    saved = {table: rows(store, table) for table in ('entities', 'local_state', 'connector_runs', 'app_meta')}
    original_lookup = store._legacy_activity_state
    calls = []

    def fail_on_second(connection, storage, raw, version):
        calls.append(raw['id'])
        if len(calls) == 2:
            assert connection.total_changes >= 1
            raise RuntimeError('Synthetic migration lookup failure')
        return original_lookup(connection, storage, raw, version)

    monkeypatch.setattr(store, '_legacy_activity_state', fail_on_second)
    advance()
    with pytest.raises(RuntimeError, match='Synthetic migration lookup failure'):
        store.bind_legacy_read_state('jira', binding()['scope'], apply=True)
    assert len(calls) == 2
    assert {table: rows(store, table) for table in saved} == saved
