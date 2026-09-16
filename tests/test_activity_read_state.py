import asyncio
import hashlib
import json
from datetime import timedelta

import pytest

from config import JiraSettings
from connectors.jira import JiraConnector
from models import ConnectorHealth, ConnectorResult
from store import Store
from tests.test_store import NOW, make_item, result


def use_legacy_version(store, event):
    """Seed the full-payload hash used by releases before this fix."""
    raw = event.model_dump(mode='json')
    raw['unread'] = False
    payload = json.dumps(raw, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    legacy = hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]
    with store._connect() as connection:
        row = connection.execute('SELECT version_hash FROM entities WHERE entity_id = ?', (event.id,)).fetchone()
        connection.execute('UPDATE local_state SET seen_version = ? WHERE entity_id = ? AND seen_version = ?',
                           (legacy, event.id, row['version_hash']))
        connection.execute('UPDATE entities SET version_hash = ? WHERE entity_id = ?', (legacy, event.id))


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('state', ['read', 'unread', 'hidden_read', 'hidden_unread'])
def test_relationship_changes_preserve_state_across_refresh_and_reopen(tmp_path, legacy, state):
    path = tmp_path / 'dashboard.sqlite3'
    store = Store(path)
    store.initialize()
    snapshot = result(make_item())
    event = snapshot.activities[0]
    event.reasons = ['assigned', 'participant', 'replied']
    store.replace_connector(snapshot, 60)
    if legacy:
        use_legacy_version(store, event)
    if state.endswith('unread'):
        store.set_local_state(event.id, 'unread')
    undo = store.dismiss_updates(event.id) if state.startswith('hidden') else []
    for reasons in (['assigned'], ['watching', 'assigned'], ['assigned', 'watching']):
        event.reasons = reasons
        store.replace_connector(snapshot, 60)
        store = Store(path)
        store.initialize()
        visible = store.load()[1]
        if undo:
            assert visible == []
            current = store.load_dismissed()[0]
            assert current['reasons'] == reasons
            assert current['unread'] == state.endswith('unread')
        else:
            assert visible[0].reasons == reasons
            assert visible[0].unread == state.endswith('unread')
    if undo:
        store.restore_dismissed(undo)
        assert store.load_dismissed() == []
        assert store.load()[1][0].unread == state.endswith('unread')


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('change', [
    {'summary': 'Edited source comment'},
    {'changes': ['Status: Active -> Blocked']},
    {'actor': 'Another engineer'},
    {'timestamp': NOW + timedelta(minutes=1)},
])
def test_real_event_changes_resurface_read_hidden_events(tmp_path, legacy, change):
    store = Store(tmp_path / 'dashboard.sqlite3')
    store.initialize()
    snapshot = result(make_item())
    store.replace_connector(snapshot, 60)
    event = snapshot.activities[0]
    if legacy:
        use_legacy_version(store, event)
    store.dismiss_updates(event.id)
    snapshot.activities[0] = event.model_copy(update={**change, 'reasons': ['watching']})
    store.replace_connector(snapshot, 60)
    assert store.load()[1][0].unread
    assert store.load_dismissed() == []


def test_legacy_unacknowledged_change_is_not_silently_marked_read(tmp_path):
    store = Store(tmp_path / 'dashboard.sqlite3')
    store.initialize()
    snapshot = result(make_item())
    event = snapshot.activities[0]
    store.replace_connector(snapshot, 60)
    use_legacy_version(store, event)
    store.dismiss_updates(event.id)
    # Simulate a legacy source change after reading and hiding the old version.
    changed = event.model_copy(update={'summary': 'New source content'})
    raw = changed.model_dump(mode='json')
    payload = json.dumps(raw, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    version = hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]
    with store._connect() as connection:
        connection.execute('UPDATE entities SET payload_json = ?, version_hash = ? WHERE entity_id = ?',
                           (payload, version, event.id))
    store = Store(store.database_path)
    store.initialize()
    snapshot.activities[0] = changed.model_copy(update={'reasons': ['watching']})
    store.replace_connector(snapshot, 60)
    assert store.load()[1][0].unread
    assert store.load_dismissed() == []


def test_jira_participation_expiry_does_not_reopen_old_update(tmp_path, monkeypatch):
    connector = JiraConnector(JiraSettings(site='https://example.atlassian.net'), 30)
    identity = {'account_id': 'me'}
    record = {'key': 'ENG-1', 'fields': {
        'summary': 'Example issue', 'updated': (NOW - timedelta(days=20)).isoformat(),
        'status': {'name': 'Resolved', 'statusCategory': {'name': 'Done'}},
        'comment': {'comments': [
            {'id': 'self', 'created': (NOW - timedelta(days=89)).isoformat(),
             'author': {'accountId': 'me'}, 'body': 'Investigating'},
            {'id': 'reply', 'created': (NOW - timedelta(days=20)).isoformat(),
             'author': {'accountId': 'other'}, 'body': 'Fixed'},
        ]},
    }}

    def snapshot(at):
        monkeypatch.setattr('connectors.jira.utc_now', lambda: at)
        items, events, _ = asyncio.run(connector._normalize(
            '', {'ENG-1': record}, {'ENG-1': {'assigned', 'watching', 'author'}}, identity))
        return ConnectorResult(connector='jira', work_items=items, activities=events,
                               health=ConnectorHealth(connector='jira', state='ok', message='ok'))

    store = Store(tmp_path / 'dashboard.sqlite3')
    store.initialize()
    before = snapshot(NOW)
    store.replace_connector(before, 60)
    event = before.activities[0]
    assert event.reasons == ['assigned', 'watching', 'author', 'waiting', 'participant', 'replied']
    use_legacy_version(store, event)
    store.set_local_state(event.id, 'seen')
    after = snapshot(NOW + timedelta(days=2))
    assert after.activities[0].id == event.id
    assert after.activities[0].reasons == ['assigned', 'watching', 'author', 'waiting']
    store.replace_connector(after, 60)
    assert not any(activity.unread for activity in store.load()[1])
    # A genuine later source update must still be delivered unread.
    record['fields']['updated'] = NOW.isoformat()
    store.replace_connector(snapshot(NOW + timedelta(days=2)), 60)
    assert sum(activity.unread for activity in store.load()[1]) == 1
