from datetime import timedelta
from activity_details import ado_changes
from tests.test_store import make_item, result, NOW
from store import Store


def test_source_details_do_not_copy_rich_text_or_raw_identities():
    details = ado_changes({'fields': {
        'System.State': {'oldValue': 'New', 'newValue': 'Active'},
        'System.AssignedTo': {'oldValue': None, 'newValue': {'displayName': 'Example Engineer', 'uniqueName': 'private@example.test'}},
        'System.Description': {'newValue': '<p>private full body</p>'},
    }})
    assert details == ['Status: New → Active', 'Assignee: None → Example Engineer', 'Description changed']
    assert 'private' not in str(details)


def test_snapshot_changes_persist_across_refreshes_and_do_not_invent_baseline(tmp_path):
    store = Store(tmp_path / 'test.sqlite3')
    store.initialize()
    store.replace_connector(result(make_item()), 60)
    assert store.load()[1][0].changes == []
    next_result = result(make_item('Blocked', NOW + timedelta(hours=1)))
    store.replace_connector(next_result, 60)
    event = next(event for event in store.load()[1] if event.timestamp == NOW + timedelta(hours=1))
    assert event.changes == ['Status: Active → Blocked']
    assert event.detail_source == 'Between local refreshes'
    store.set_local_state(event.id, 'seen')
    store.replace_connector(result(make_item('Blocked', NOW + timedelta(hours=1))), 60)
    event = next(event for event in store.load()[1] if event.timestamp == NOW + timedelta(hours=1))
    assert event.changes == ['Status: Active → Blocked']
    assert not event.unread
