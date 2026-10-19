import hashlib
import json
import sqlite3
from datetime import timedelta

import pytest

from models import Activity, ConnectorHealth, ConnectorResult, WorkItem, utc_now
from store import Store
from tests.test_cache_scopes import binding


NOW = utc_now() - timedelta(days=1)
REALM = 'org-one'
ACCOUNT = 'engineer@example.test'


def scoped_store(path, account=ACCOUNT):
    store = Store(path, {'azure_devops': binding('azure_devops', REALM, account)})
    store.initialize()
    return store


def item(key='42', when=NOW):
    return WorkItem(
        id=f'ado:workitem:{key}', source='ado', source_type='User Story', project='Example',
        key=key, title=f'Work {key}', url=f'https://example.test/item/{key}', status='Active',
        updated_at=when, reasons=['assigned'],
    )


def placeholder(parent=None):
    parent = parent or item()
    return Activity(
        id=f'{parent.id}:updated:{parent.updated_at.isoformat()}', source='ado', event_type='updated',
        item_id=parent.id, item_key=parent.key, item_title=parent.title,
        timestamp=parent.updated_at, summary='User Story updated', url=parent.url,
        reasons=parent.reasons,
    )


def captured(parent=None, update_id=22, *, with_alias=True, **changes):
    parent = parent or item()
    legacy = placeholder(parent)
    return legacy.model_copy(update={
        'id': f'{parent.id}:update:{update_id}', 'source_update_id': update_id,
        'source_revision': 16, 'legacy_activity_id': legacy.id if with_alias else None,
        'detail_status': 'captured', 'summary': 'Target Date: Jul 6 -> Sep 1',
        'changes': ['Target Date: Jul 6 -> Sep 1'], 'detail_source': 'Azure DevOps history',
        'actor': 'Engineer', **changes,
    })


def snapshot(*events, work=None, checkpoint=None, account=ACCOUNT):
    return ConnectorResult(
        connector='azure_devops', cache_scope=binding('azure_devops', REALM, account)['scope'],
        work_items=[item()] if work is None else work, activities=list(events), checkpoint=checkpoint,
        health=ConnectorHealth(connector='azure_devops', state='ok', message='Synthetic',
                               last_success_at=NOW),
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
def test_exact_placeholder_enrichment_preserves_triage_version_and_undo(tmp_path, state, legacy_hash):
    path = tmp_path / 'history.db'
    store = scoped_store(path)
    generic = placeholder()
    store.replace_connector(snapshot(generic), 60)
    event_id = store.load()[1][0].id
    if legacy_hash:
        # Older deployments hashed a full payload without today's optional fields.
        row = activity_rows(store)[0]
        raw = json.loads(row['payload_json'])
        for name in ('detail_status', 'source_update_id', 'source_revision', 'legacy_activity_id'):
            raw.pop(name, None)
        payload = json.dumps(raw, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        version = hashlib.sha256(payload.encode()).hexdigest()[:24]
        with store._connect() as connection:
            connection.execute('UPDATE entities SET payload_json = ?, version_hash = ? WHERE entity_id = ?',
                               (payload, version, event_id))
            connection.execute('UPDATE local_state SET seen_version = ? WHERE entity_id = ?',
                               (version, event_id))
    if state.endswith('unread'):
        store.set_local_state(event_id, 'unread')
    undo = store.dismiss_updates(event_id) if state.startswith('hidden') else []
    before_state = local_state(store, event_id)
    before_version = activity_rows(store)[0]['version_hash']

    # The connector can return the current generic event before recovered history.
    store.replace_connector(snapshot(generic, captured(), checkpoint={'cursor': 22}), 60)
    store = scoped_store(path)
    assert len(activity_rows(store)) == 1
    row = activity_rows(store)[0]
    assert row['entity_id'] == event_id
    assert row['version_hash'] == before_version
    assert local_state(store, event_id) == before_state
    raw = json.loads(row['payload_json'])
    assert raw['source_update_id'] == 22
    assert raw['changes'] == ['Target Date: Jul 6 -> Sep 1']
    assert store.load_connector_state('azure_devops') == {'cursor': 22}

    # Replays enrich this immutable update, and generic-only cursor refreshes
    # cannot erase its provenance or recreate another generic placeholder.
    store.replace_connector(snapshot(captured(actor='Verified engineer')), 60)
    store.replace_connector(snapshot(generic), 60)
    assert len(activity_rows(store)) == 1
    assert local_state(store, event_id) == before_state
    assert activity_rows(store)[0]['version_hash'] == before_version
    assert json.loads(activity_rows(store)[0]['payload_json'])['actor'] == 'Verified engineer'
    if undo:
        assert store.load()[1] == []
        assert store.load_dismissed()[0]['unread'] == state.endswith('unread')
        store.restore_dismissed(undo)
    assert store.load()[1][0].unread == state.endswith('unread')


@pytest.mark.parametrize('state', ['read', 'unread', 'hidden_read', 'hidden_unread'])
def test_pending_failed_then_captured_preserves_existing_state_at_every_refresh(tmp_path, state):
    path = tmp_path / 'history.db'
    store = scoped_store(path)
    legacy = placeholder()
    assert legacy.summary == f'{item().source_type} updated'
    store.replace_connector(snapshot(legacy), 60)
    event_id = store.load()[1][0].id
    if state.endswith('unread'):
        store.set_local_state(event_id, 'unread')
    undo = store.dismiss_updates(event_id) if state.startswith('hidden') else []
    before_state = local_state(store, event_id)
    before_version = activity_rows(store)[0]['version_hash']
    for status in ('pending', 'failed'):
        event = legacy.model_copy(update={'detail_status': status})
        store.replace_connector(snapshot(event), 60)
        store = scoped_store(path)
        assert len(activity_rows(store)) == 1
        assert local_state(store, event_id) == before_state
        assert activity_rows(store)[0]['version_hash'] == before_version
        raw = json.loads(activity_rows(store)[0]['payload_json'])
        assert raw['summary'] == f'{item().source_type} updated'
        assert raw['detail_status'] == status
        visible = store.load_dismissed() if undo else store.load()[1]
        assert len(visible) == 1
        assert (visible[0]['unread'] if undo else visible[0].unread) == state.endswith('unread')
    store.replace_connector(snapshot(captured()), 60)
    store = scoped_store(path)
    assert len(activity_rows(store)) == 1
    assert local_state(store, event_id) == before_state
    assert activity_rows(store)[0]['version_hash'] == before_version
    raw = json.loads(activity_rows(store)[0]['payload_json'])
    assert raw['source_update_id'] == 22
    assert raw['detail_status'] == 'captured'
    if undo:
        assert store.load()[1] == []
        store.restore_dismissed(undo)
    assert store.load()[1][0].unread == state.endswith('unread')


@pytest.mark.parametrize('canonical_first', [False, True])
def test_canonical_and_generic_order_never_creates_proven_duplicate(tmp_path, canonical_first):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(), 60)
    pair = [captured(), placeholder()] if canonical_first else [placeholder(), captured()]
    store.replace_connector(snapshot(*pair), 60)
    assert len(activity_rows(store)) == 1
    original = store.load()[1][0]
    undo = store.dismiss_updates(original.id)
    before = local_state(store, original.id)
    # Reverse order on replay: stored identity and both local-state tokens win.
    store.replace_connector(snapshot(*reversed(pair)), 60)
    assert len(activity_rows(store)) == 1
    assert local_state(store, original.id) == before
    assert store.load()[1] == []
    store.restore_dismissed(undo)
    assert store.load()[1][0].id == original.id


def test_enrichment_keeps_stale_acknowledgements_and_delivers_new_source_update_unread(tmp_path):
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
    later = item(when=NOW + timedelta(minutes=1))
    store.replace_connector(snapshot(captured(later, 23), work=[later]), 60)
    assert len(store.load()[1]) == 2
    assert all(event.unread for event in store.load()[1])


def test_history_without_exact_alias_is_new_and_timestamp_cannot_transfer_state(tmp_path):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(placeholder()), 60)
    original = store.load()[1][0]
    store.dismiss_updates(original.id)
    event = captured(with_alias=False)
    store.replace_connector(snapshot(event), 60)
    assert len(activity_rows(store)) == 2
    assert store.load()[1][0].source_update_id == 22
    assert store.load()[1][0].unread
    assert store.load_dismissed()[0]['id'] == original.id


@pytest.mark.parametrize('changes', [
    {'legacy_activity_id': 'ado:workitem:42:updated:wrong'},
    {'timestamp': NOW + timedelta(seconds=1)},
    {'item_id': 'ado:workitem:other', 'item_key': 'other'},
    {'detail_status': 'pending'},
])
def test_alias_requires_authoritative_parent_timestamp_and_capture_proof(tmp_path, changes):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(placeholder()), 60)
    original = store.load()[1][0]
    store.dismiss_updates(original.id)
    store.replace_connector(snapshot(captured(**changes)), 60)
    assert len(activity_rows(store)) == 2
    assert store.load_dismissed()[0]['id'] == original.id
    assert store.load()[1][0].unread


def test_conflicting_immutable_update_id_does_not_adopt_alias_triage(tmp_path):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(placeholder()), 60)
    store.replace_connector(snapshot(captured()), 60)
    original = store.load()[1][0]
    store.dismiss_updates(original.id)
    store.replace_connector(snapshot(captured(update_id=23)), 60)
    assert len(activity_rows(store)) == 2
    assert store.load_dismissed()[0]['id'] == original.id
    assert store.load()[1][0].source_update_id == 23
    assert store.load()[1][0].unread


def test_canonical_event_suppresses_later_generic_snapshot_without_existing_placeholder(tmp_path):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(), 60)
    store.replace_connector(snapshot(captured()), 60)
    original = store.load()[1][0]
    store.dismiss_updates(original.id)
    store.replace_connector(snapshot(placeholder()), 60)
    assert len(activity_rows(store)) == 1
    assert store.load()[1] == []
    assert store.load_dismissed()[0]['id'] == original.id


def test_administrative_event_replaces_placeholder_without_snapshot_inference(tmp_path):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(placeholder()), 60)
    original = store.load()[1][0]
    store.dismiss_updates(original.id)
    parent = item().model_copy(update={'status': 'Closed', 'updated_at': NOW + timedelta(minutes=1)})
    administrative = captured(detail_status='administrative', changes=[], summary='Administrative fields updated')
    store.replace_connector(snapshot(administrative, work=[parent]), 60)
    hidden = store.load_dismissed()[0]
    assert hidden['id'] == original.id
    assert hidden['changes'] == []
    assert hidden['detail_status'] == 'administrative'
    assert hidden['summary'] == 'Administrative fields updated'


def test_ado_comment_edits_keep_existing_version_change_semantics(tmp_path):
    store = scoped_store(tmp_path / 'history.db')
    comment = placeholder().model_copy(update={'event_type': 'commented', 'id': f'{item().id}:comment:1',
                                               'summary': 'Original source comment'})
    store.replace_connector(snapshot(comment), 60)
    original = store.load()[1][0]
    store.dismiss_updates(original.id)
    store.replace_connector(snapshot(comment.model_copy(update={'summary': 'Edited source comment'})), 60)
    assert store.load_dismissed() == []
    assert store.load()[1][0].unread


def test_first_install_baseline_behavior_is_unchanged(tmp_path):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(captured()), 60)
    assert not store.load()[1][0].unread


def test_default_capture_metadata_preserves_historical_content_hash(tmp_path):
    event = placeholder().model_copy(update={'source': 'jira', 'id': 'jira:event'})
    current = event.model_dump(mode='json')
    legacy = {key: value for key, value in current.items()
              if key not in {'detail_status', 'source_update_id', 'source_revision', 'legacy_activity_id'}}
    historical = {key: value for key, value in legacy.items() if key not in {'reasons', 'unread'}}
    expected = hashlib.sha256(json.dumps(historical, sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=False).encode()).hexdigest()[:24]
    assert Store._activity_version(current) == expected
    assert Store._activity_version(legacy) == expected


def test_checkpoint_and_entities_commit_together_and_rollback_on_checkpoint_failure(tmp_path):
    path = tmp_path / 'history.db'
    store = scoped_store(path)
    store.replace_connector(snapshot(placeholder(), checkpoint={'cursor': 1}), 60)
    before = database_rows(store)
    scope = binding('azure_devops', REALM, ACCOUNT)['scope']
    with store._connect() as connection:
        connection.execute(f"""CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON app_meta
            WHEN NEW.key = 'connector_state:azure_devops@{scope}'
            BEGIN SELECT RAISE(ABORT, 'checkpoint rejected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='checkpoint rejected'):
        store.replace_connector(snapshot(captured(), checkpoint={'cursor': 22}), 60)
    assert database_rows(store) == before
    store = scoped_store(path)
    assert store.load_connector_state('azure_devops') == {'cursor': 1}
    with store._connect() as connection:
        connection.execute('DROP TRIGGER reject_checkpoint')
    store.replace_connector(snapshot(captured(), checkpoint={'cursor': 22}), 60)
    assert store.load_connector_state('azure_devops') == {'cursor': 22}
    assert json.loads(activity_rows(store)[0]['payload_json'])['source_update_id'] == 22
    # An optional checkpoint does not erase existing progress.
    store.replace_connector(snapshot(placeholder()), 60)
    assert store.load_connector_state('azure_devops') == {'cursor': 22}


def test_checkpoint_rejects_non_dictionary_without_mutation(tmp_path):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(placeholder(), checkpoint={'cursor': 1}), 60)
    before = database_rows(store)
    invalid = snapshot(captured()).model_copy(update={'checkpoint': []})
    with pytest.raises(TypeError, match='dictionary'):
        store.replace_connector(invalid, 60)
    assert database_rows(store) == before


def test_inventory_includes_hidden_retained_parents_without_reactivating_or_crossing_scope(tmp_path):
    path = tmp_path / 'history.db'
    store = scoped_store(path)
    retained = item()
    obsolete = item('43')
    store.replace_connector(snapshot(placeholder(), work=[retained, obsolete], checkpoint={'cursor': 1}), 60)
    work, events, _ = store.load()
    store.hide_work(next(parent.id for parent in work if parent.key == '42'))
    store.dismiss_updates(events[0].id)
    store.replace_connector(snapshot(work=[]), 60)
    before = database_rows(store)
    inventory = store.history_inventory(cache_scope=binding('azure_devops', REALM, ACCOUNT)['scope'])
    assert [parent['id'] for parent in inventory] == [retained.id]
    assert inventory[0]['metadata']['snapshot_only']
    assert inventory[0]['metadata']['latest_activity_at'] == placeholder().model_dump(mode='json')['timestamp']
    assert inventory[0]['status'] == 'Active'
    assert database_rows(store) == before
    other = scoped_store(path, 'other@example.test')
    assert other.history_inventory(cache_scope=binding('azure_devops', REALM, 'other@example.test')['scope']) == []
    with pytest.raises(ValueError, match='scope'):
        other.history_inventory(cache_scope=binding('azure_devops', REALM, ACCOUNT)['scope'])
    with pytest.raises(ValueError, match='verified'):
        other.history_inventory()
    other.replace_connector(snapshot(captured(), checkpoint={'cursor': 99}, account='other@example.test'), 60)
    assert len(other.load()[1]) == 1
    assert store.load_connector_state('azure_devops') == {'cursor': 1}
    hidden_parent = next(row for row in store.load_dismissed() if row['entity_kind'] == 'work_item')
    assert hidden_parent['metadata']['snapshot_only']
    assert len(store.load_dismissed()) == 2


def test_inventory_reports_recent_activity_for_stale_parent_snapshot(tmp_path):
    store = scoped_store(tmp_path / 'history.db')
    stale = item(when=NOW - timedelta(days=90))
    store.replace_connector(snapshot(placeholder(), work=[stale]), 120)
    store.replace_connector(snapshot(work=[]), 120)
    inventory = store.history_inventory(cache_scope=binding('azure_devops', REALM, ACCOUNT)['scope'])
    assert inventory[0]['updated_at'] == stale.model_dump(mode='json')['updated_at']
    assert inventory[0]['metadata']['latest_activity_at'] == placeholder().model_dump(mode='json')['timestamp']


def test_backfill_does_not_reactivate_removed_parent_or_restore_hidden_work(tmp_path):
    store = scoped_store(tmp_path / 'history.db')
    store.replace_connector(snapshot(placeholder()), 60)
    work, events, _ = store.load()
    store.hide_work(work[0].id)
    undo = store.dismiss_updates(events[0].id)
    store.replace_connector(snapshot(work=[]), 60)
    state = database_rows(store)['local_state']
    store.replace_connector(snapshot(captured(), work=[], checkpoint={'cursor': 22}), 60)
    assert database_rows(store)['local_state'] == state
    assert store.load()[0] == []
    assert store.load()[1] == []
    hidden = store.load_dismissed()
    assert len(hidden) == 2
    parent = next(row for row in hidden if row['entity_kind'] == 'work_item')
    assert parent['metadata']['snapshot_only']
    assert parent['status'] == 'Active'
    store.restore_dismissed(undo)
    assert store.load()[0] == []
    assert store.load()[1][0].changes == ['Target Date: Jul 6 -> Sep 1']
