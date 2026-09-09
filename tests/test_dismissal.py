from datetime import timedelta
from tests.test_store import make_item, result, NOW
from store import Store


def populated(tmp_path):
    store = Store(tmp_path / 'restore.sqlite3')
    store.initialize()
    store.replace_connector(result(make_item()), 60)
    update = result(make_item('Blocked', NOW + timedelta(hours=1)))
    store.replace_connector(update, 60)
    return store, update


def test_one_update_dismiss_does_not_hide_work_or_other_events_and_undo_preserves_unread(tmp_path):
    store, update = populated(tmp_path)
    target = update.activities[0]
    undo = store.dismiss_updates(target.id)
    assert len(undo) == 1
    assert len(store.load()[0]) == 1
    assert len(store.load()[1]) == 1
    assert store.load_dismissed()[0]['id'] == target.id
    store.restore_dismissed(undo)
    restored = next(a for a in store.load()[1] if a.id == target.id)
    assert restored.unread
    assert store.load_dismissed() == []


def test_bulk_dismiss_only_existing_updates_for_exact_item_and_future_updates_arrive(tmp_path):
    store, update = populated(tmp_path)
    other_item = make_item().model_copy(update={'id': 'jira:issue:ENG-2', 'key': 'ENG-2'})
    unrelated = result(other_item)
    unrelated.activities[0] = unrelated.activities[0].model_copy(update={'id': 'other-event', 'item_id': other_item.id})
    update.work_items.append(other_item)
    update.activities.extend(unrelated.activities)
    store.replace_connector(update, 60)
    undo = store.dismiss_updates(update.activities[0].id, True)
    assert len(undo) == 2
    assert [a.id for a in store.load()[1]] == ['other-event']
    assert len(store.load()[0]) == 2
    future = result(make_item('Ready', NOW + timedelta(hours=2)))
    store.replace_connector(future, 60)
    assert future.activities[0].id in [a.id for a in store.load()[1]]
    assert store.dismiss_updates(future.work_items[0].id) == []


def test_restore_legacy_work_item_and_seen_update(tmp_path):
    store, update = populated(tmp_path)
    item = update.work_items[0]
    store.set_local_state(item.id, 'dismiss')
    assert store.load()[0] == []
    assert any(row['entity_kind'] == 'work_item' for row in store.load_dismissed())
    assert store.set_local_state(item.id, 'restore')
    assert len(store.load()[0]) == 1
    event = update.activities[0]
    store.set_local_state(event.id, 'seen')
    undo = store.dismiss_updates(event.id)
    store.restore_dismissed(undo)
    assert not next(a for a in store.load()[1] if a.id == event.id).unread


def test_undo_does_not_clear_a_later_version_dismissal(tmp_path):
    store, update = populated(tmp_path)
    original = store.dismiss_updates(update.activities[0].id)
    update.activities[0].summary = 'Additional source details'
    update.activities[0].changes = ['Priority: 2 -> 1']
    update.activities[0].detail_source = 'ADO revision history'
    store.replace_connector(update, 60)
    later = store.dismiss_updates(update.activities[0].id)
    assert original != later
    store.restore_dismissed(original)
    assert len(store.load_dismissed()) == 1
    store.restore_dismissed(later)
    assert store.load_dismissed() == []
