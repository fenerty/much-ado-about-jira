import hashlib
import json
import sqlite3
from datetime import timedelta, timezone

import pytest

from models import Activity, ConnectorHealth, ConnectorResult, WorkItem, utc_now
from store import Store
from tests.test_cache_scopes import binding


NOW = utc_now() - timedelta(days=1)
SITE = 'https://one.atlassian.net'
ACCOUNT = 'engineer@example.test'


def scoped_store(path, account=ACCOUNT):
    store = Store(path, {'jira': binding('jira', SITE, account)})
    store.initialize()
    return store


def item(key='ENG-42', when=NOW):
    return WorkItem(
        id=f'jira:issue:{key}', source='jira', source_type='Story', project='ENG',
        key=key, title=f'Work {key}', url=f'{SITE}/browse/{key}', status='Open',
        updated_at=when, reasons=['authored'],
    )


def placeholder(parent=None):
    parent = parent or item()
    return Activity(
        id=f'{parent.id}:updated:{parent.updated_at.isoformat()}', source='jira', event_type='updated',
        item_id=parent.id, item_key=parent.key, item_title=parent.title,
        timestamp=parent.updated_at, summary='Story updated', url=parent.url,
        reasons=parent.reasons,
    )


def captured(parent=None, change_id='opaque-22', *, with_alias=True, **changes):
    parent = parent or item()
    legacy = placeholder(parent)
    return legacy.model_copy(update={
        'id': f'{parent.id}:change:{change_id}', 'source_update_id': change_id,
        'legacy_activity_id': legacy.id if with_alias else None,
        'detail_status': 'captured', 'summary': 'Status: Open -> In Progress',
        'changes': ['Status: Open -> In Progress'], 'detail_source': 'Jira change history',
        'actor': 'Engineer', **changes,
    })


def snapshot(*events, work=None, checkpoint=None, account=ACCOUNT):
    return ConnectorResult(
        connector='jira', cache_scope=binding('jira', SITE, account)['scope'],
        work_items=[item()] if work is None else work, activities=list(events), checkpoint=checkpoint,
        health=ConnectorHealth(connector='jira', state='ok', message='Synthetic', last_success_at=NOW),
    )


def database_rows(store):
    with store._connect() as connection:
        return {table: [dict(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY 1')]
                for table in ('entities', 'local_state', 'app_meta', 'connector_runs')}


def activity_rows(store):
    return [row for row in database_rows(store)['entities'] if row['entity_kind'] == 'activity']


def local_state(store, entity_id):
    return next(row for row in database_rows(store)['local_state'] if row['entity_id'] == entity_id)


@pytest.mark.parametrize('state', ['read', 'unread', 'hidden_read', 'hidden_unread'])
@pytest.mark.parametrize('legacy_hash', [False, True])
def test_changelog_enrichment_preserves_triage_hash_and_pending_undo(tmp_path, state, legacy_hash):
    path = tmp_path / 'history.db'
    store = scoped_store(path)
    generic = placeholder()
    old_parent = item(when=NOW - timedelta(minutes=1))
    store.replace_connector(snapshot(generic, work=[old_parent]), 60)
    event_id = store.load()[1][0].id
    if legacy_hash:
        row = activity_rows(store)[0]
        raw = json.loads(row['payload_json'])
        for name in ('detail_status', 'source_update_id', 'source_revision', 'legacy_activity_id'):
            raw.pop(name, None)
        payload = json.dumps(raw, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        version = hashlib.sha256(payload.encode()).hexdigest()[:24]
        with store._connect() as connection:
            connection.execute('UPDATE entities SET payload_json = ?, version_hash = ? WHERE entity_id = ?',
                               (payload, version, event_id))
            connection.execute('UPDATE local_state SET seen_version = ? WHERE entity_id = ?', (version, event_id))
    if state.endswith('unread'):
        store.set_local_state(event_id, 'unread')
    undo = store.dismiss_updates(event_id) if state.startswith('hidden') else []
    before_state = local_state(store, event_id)
    before_version = activity_rows(store)[0]['version_hash']
    new_parent = item().model_copy(update={'status': 'Closed'})
    # A generic snapshot arriving first must not infer a new version before
    # authoritative history enriches the exact retained event.
    store.replace_connector(snapshot(generic, captured(), work=[new_parent], checkpoint={'changes': 22}), 60)
    store = scoped_store(path)
    assert len(activity_rows(store)) == 1
    row = activity_rows(store)[0]
    assert row['entity_id'] == event_id
    assert row['version_hash'] == before_version
    assert local_state(store, event_id) == before_state
    assert json.loads(row['payload_json'])['source_update_id'] == 'opaque-22'
    assert store.load_connector_state('jira') == {'changes': 22}

    store.replace_connector(snapshot(captured(actor='Verified engineer')), 60)
    store.replace_connector(snapshot(generic.model_copy(update={'detail_status': 'failed'})), 60)
    assert len(activity_rows(store)) == 1
    assert local_state(store, event_id) == before_state
    raw = json.loads(activity_rows(store)[0]['payload_json'])
    assert raw['actor'] == 'Verified engineer'
    assert raw['detail_status'] == 'captured'
    assert raw['changes'] == ['Status: Open -> In Progress']
    if undo:
        assert store.load()[1] == []
        assert store.load_dismissed()[0]['unread'] == state.endswith('unread')
        store.restore_dismissed(undo)
    assert store.load()[1][0].unread == state.endswith('unread')


@pytest.mark.parametrize('status', ['pending', 'failed'])
def test_changelog_failure_status_keeps_generic_read_hidden_and_undo_state(tmp_path, status):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(placeholder()), 60)
    event_id = store.load()[1][0].id
    undo = store.dismiss_updates(event_id)
    before = local_state(store, event_id)
    version = activity_rows(store)[0]['version_hash']
    store.replace_connector(snapshot(placeholder().model_copy(update={'detail_status': status})), 60)
    assert local_state(store, event_id) == before
    assert activity_rows(store)[0]['version_hash'] == version
    assert store.load_dismissed()[0]['detail_status'] == status
    store.restore_dismissed(undo)
    assert not store.load()[1][0].unread


@pytest.mark.parametrize('canonical_first', [False, True])
def test_initial_baseline_and_replay_coalesce_proven_snapshot_only_once(tmp_path, canonical_first):
    store = scoped_store(tmp_path / 'history.db')
    pair = [captured(), placeholder()] if canonical_first else [placeholder(), captured()]
    store.replace_connector(snapshot(*pair), 60)
    assert len(activity_rows(store)) == 1
    original = store.load()[1][0]
    assert not original.unread
    undo = store.dismiss_updates(original.id)
    before = local_state(store, original.id)
    store.replace_connector(snapshot(*reversed(pair)), 60)
    assert len(activity_rows(store)) == 1
    assert local_state(store, original.id) == before
    store.restore_dismissed(undo)
    assert store.load()[1][0].id == original.id


def test_same_timestamp_distinct_changelog_ids_preserve_only_proven_alias_state(tmp_path):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(placeholder()), 60)
    original = store.load()[1][0]
    undo = store.dismiss_updates(original.id)
    before = local_state(store, original.id)
    store.replace_connector(snapshot(captured(), captured(change_id='opaque-23'), placeholder()), 60)
    assert len(activity_rows(store)) == 2
    assert store.load_dismissed()[0]['source_update_id'] == 'opaque-22'
    assert store.load()[1][0].source_update_id == 'opaque-23'
    assert store.load()[1][0].unread
    store.replace_connector(snapshot(captured(change_id='opaque-23'), captured(), placeholder()), 60)
    assert len(activity_rows(store)) == 2
    assert local_state(store, original.id) == before
    store.restore_dismissed(undo)
    assert {event.source_update_id: event.unread for event in store.load()[1]} == {
        'opaque-22': False, 'opaque-23': True,
    }


def test_enrichment_does_not_promote_stale_acknowledgements(tmp_path):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(placeholder()), 60)
    event_id = store.load()[1][0].id
    with store._connect() as connection:
        connection.execute("UPDATE local_state SET seen_version = 'older', dismissed_version = 'older' WHERE entity_id = ?",
                           (event_id,))
    before = local_state(store, event_id)
    store.replace_connector(snapshot(captured()), 60)
    assert local_state(store, event_id) == before
    assert store.load()[1][0].unread
    assert store.load_dismissed() == []


def test_equivalent_jira_timestamp_offsets_preserve_retained_id_and_undo(tmp_path):
    store = scoped_store(tmp_path / 'history.db')
    original_parent = item(when=NOW.astimezone(timezone(timedelta(hours=-4))))
    original_placeholder = placeholder(original_parent)
    store.replace_connector(snapshot(original_placeholder, work=[original_parent]), 60)
    event_id = store.load()[1][0].id
    undo = store.dismiss_updates(event_id)
    state = local_state(store, event_id)
    store.replace_connector(snapshot(captured(), placeholder()), 60)
    assert len(activity_rows(store)) == 1
    assert activity_rows(store)[0]['entity_id'] == event_id
    assert local_state(store, event_id) == state
    store.replace_connector(snapshot(original_placeholder), 60)
    assert len(activity_rows(store)) == 1
    assert store.load_dismissed()[0]['source_update_id'] == 'opaque-22'
    store.restore_dismissed(undo)
    assert not store.load()[1][0].unread


@pytest.mark.parametrize('changes', [
    {'legacy_activity_id': None},
    {'legacy_activity_id': 'jira:issue:ENG-42:updated:wrong'},
    {'id': 'jira:issue:ENG-42:change:wrong'},
    {'timestamp': NOW + timedelta(seconds=1)},
    {'item_id': 'jira:issue:ENG-43', 'item_key': 'ENG-43'},
    {'source_update_id': 22},
    {'source_update_id': ''},
    {'detail_status': 'pending'},
    {'detail_source': 'Between local refreshes'},
])
def test_alias_requires_proven_jira_change_identity_parent_and_timestamp(tmp_path, changes):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(placeholder()), 60)
    original = store.load()[1][0]
    store.dismiss_updates(original.id)
    store.replace_connector(snapshot(captured(**changes)), 60)
    assert len(activity_rows(store)) == 2
    assert store.load_dismissed()[0]['id'] == original.id
    assert store.load()[1][0].unread


def test_administrative_change_never_infers_snapshot_fields(tmp_path):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(placeholder()), 60)
    original = store.load()[1][0]
    store.dismiss_updates(original.id)
    later = item(when=NOW + timedelta(minutes=1)).model_copy(update={'status': 'Closed'})
    event = captured(detail_status='administrative', changes=[], summary='Administrative fields updated')
    store.replace_connector(snapshot(event, work=[later]), 60)
    hidden = store.load_dismissed()[0]
    assert hidden['id'] == original.id
    assert hidden['changes'] == []
    assert hidden['detail_status'] == 'administrative'


def test_jira_history_inventory_includes_hidden_inactive_retained_parents_and_isolates_account(tmp_path):
    path = tmp_path / 'history.db'
    store = scoped_store(path)
    stale = item(when=NOW - timedelta(days=90))
    obsolete = item('ENG-43')
    store.replace_connector(snapshot(placeholder(), work=[stale, obsolete]), 120)
    work, events, _ = store.load()
    store.hide_work(next(parent.id for parent in work if parent.key == stale.key))
    undo = store.dismiss_updates(events[0].id)
    store.replace_connector(snapshot(work=[]), 120)
    before = database_rows(store)
    scope = binding('jira', SITE, ACCOUNT)['scope']
    inventory = store.history_inventory('jira', cache_scope=scope)
    assert [parent['id'] for parent in inventory] == [stale.id]
    assert inventory[0]['metadata']['snapshot_only']
    assert inventory[0]['updated_at'] == stale.model_dump(mode='json')['updated_at']
    assert inventory[0]['metadata']['latest_activity_at'] == placeholder().model_dump(mode='json')['timestamp']
    assert database_rows(store) == before

    store.replace_connector(snapshot(captured(), work=[]), 120)
    assert database_rows(store)['local_state'] == before['local_state']
    assert all(parent.key != stale.key for parent in store.load()[0])
    assert store.load()[1] == []
    store.restore_dismissed(undo)
    assert all(parent.key != stale.key for parent in store.load()[0])
    assert store.load()[1][0].source_update_id == 'opaque-22'

    other = scoped_store(path, 'other@example.test')
    other_scope = binding('jira', SITE, 'other@example.test')['scope']
    assert other.history_inventory('jira', cache_scope=other_scope) == []
    with pytest.raises(ValueError, match='scope'):
        other.history_inventory('jira', cache_scope=scope)
    other.replace_connector(snapshot(captured(), account='other@example.test'), 120)
    assert len(other.load()[1]) == 1
    assert store.load()[1][0].id != other.load()[1][0].id


def test_jira_checkpoint_and_changelog_enrichment_commit_or_rollback_together(tmp_path):
    path = tmp_path / 'history.db'
    store = scoped_store(path)
    store.replace_connector(snapshot(placeholder(), checkpoint={'changes': 1}), 60)
    before = database_rows(store)
    scope = binding('jira', SITE, ACCOUNT)['scope']
    with store._connect() as connection:
        connection.execute(f"""CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON app_meta
            WHEN NEW.key = 'connector_state:jira@{scope}'
            BEGIN SELECT RAISE(ABORT, 'checkpoint rejected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='checkpoint rejected'):
        store.replace_connector(snapshot(captured(), checkpoint={'changes': 22}), 60)
    assert database_rows(store) == before
    store = scoped_store(path)
    assert store.load_connector_state('jira') == {'changes': 1}
    with store._connect() as connection:
        connection.execute('DROP TRIGGER reject_checkpoint')
    store.replace_connector(snapshot(captured(), checkpoint={'changes': 22}), 60)
    assert store.load_connector_state('jira') == {'changes': 22}
    assert json.loads(activity_rows(store)[0]['payload_json'])['source_update_id'] == 'opaque-22'
    store.replace_connector(snapshot(placeholder()), 60)
    assert store.load_connector_state('jira') == {'changes': 22}


@pytest.mark.parametrize('state', ['read', 'unread', 'hidden_read', 'hidden_unread'])
@pytest.mark.parametrize('bind_first', [False, True])
def test_explicit_legacy_binding_preserves_triage_before_or_after_changelog_enrichment(tmp_path, state, bind_first):
    path = tmp_path / 'history.db'
    legacy = Store(path)
    legacy.initialize()
    generic = placeholder()
    legacy.replace_connector(snapshot(generic), 60)
    if state.endswith('unread'):
        legacy.set_local_state(generic.id, 'unread')
    if state.startswith('hidden'):
        legacy.dismiss_updates(generic.id)
    preserved = database_rows(legacy)
    store = scoped_store(path)
    scope = binding('jira', SITE, ACCOUNT)['scope']
    if bind_first:
        store.record_health(snapshot().health, cache_scope=scope)
        store.bind_legacy_read_state('jira', scope, apply=True)
        # No generic scoped row exists yet; history may arrive on its own.
        store.replace_connector(snapshot(captured()), 60)
    else:
        # Scoped enrichment retained the generic ID before the owner binds its
        # legacy state. The same proof must work in this order as well.
        store.replace_connector(snapshot(generic), 60)
        store.replace_connector(snapshot(captured()), 60)
        store.bind_legacy_read_state('jira', scope, apply=True)
    visible = store.load_dismissed() if state.startswith('hidden') else store.load()[1]
    assert len(visible) == 1
    if state.startswith('hidden'):
        assert store.load()[1] == []
        assert visible[0]['unread'] == state.endswith('unread')
        assert visible[0]['source_update_id'] == 'opaque-22'
    else:
        assert visible[0].unread == state.endswith('unread')
        assert visible[0].source_update_id == 'opaque-22'
    after = database_rows(store)
    assert [row for row in after['entities'] if row['connector'] == 'jira'] == preserved['entities']
    assert [row for row in after['local_state'] if not row['entity_id'].startswith('jira@')] == preserved['local_state']


@pytest.mark.parametrize('source', ['ado', 'jira'])
@pytest.mark.parametrize('bind_first', [False, True])
@pytest.mark.parametrize('sequential', [False, True])
def test_bound_legacy_placeholder_can_acknowledge_only_one_same_timestamp_source_change(
    tmp_path, source, bind_first, sequential,
):
    from tests import test_ado_backfill_state as ado_state

    path = tmp_path / 'history.db'
    if source == 'ado':
        make_store, make_snapshot = ado_state.scoped_store, ado_state.snapshot
        generic, first, second = ado_state.placeholder(), ado_state.captured(), ado_state.captured(update_id=23)
        connector, first_id, second_id = 'azure_devops', 22, 23
    else:
        make_store, make_snapshot = scoped_store, snapshot
        generic, first, second = placeholder(), captured(), captured(change_id='opaque-23')
        connector, first_id, second_id = 'jira', 'opaque-22', 'opaque-23'
    legacy = Store(path)
    legacy.initialize()
    legacy.replace_connector(make_snapshot(generic), 60)
    legacy.dismiss_updates(generic.id)
    store = make_store(path)
    scope = make_snapshot().cache_scope
    if bind_first:
        store.record_health(make_snapshot().health, cache_scope=scope)
        store.bind_legacy_read_state(connector, scope, apply=True)
    if sequential:
        store.replace_connector(make_snapshot(first), 60)
        store.replace_connector(make_snapshot(second), 60)
    else:
        store.replace_connector(make_snapshot(first, second), 60)
    if not bind_first:
        before = database_rows(store)
        preview = store.bind_legacy_read_state(connector, scope)
        assert database_rows(store) == before
        assert store.bind_legacy_read_state(connector, scope, apply=True) == preview
    for events in ((second, first, generic), (first, second)):
        store.replace_connector(make_snapshot(*events), 60)
        store = make_store(path)
        hidden = store.load_dismissed()
        visible = store.load()[1]
        assert len(hidden) == len(visible) == 1
        assert hidden[0]['source_update_id'] == first_id
        assert not hidden[0]['unread']
        assert visible[0].source_update_id == second_id
        assert visible[0].unread
