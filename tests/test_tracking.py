from tests.test_store import make_item, result
from store import Store
from relevance import build_dashboard


def test_tracking_survives_omission_closure_and_handoff(tmp_path):
    store = Store(tmp_path / 'test.db'); store.initialize()
    first = result(make_item()); store.replace_connector(first, 60)
    store.replace_connector(first.model_copy(update={'work_items': [], 'activities': []}), 60)
    saved = store.load()[0][0]
    assert saved.metadata['snapshot_only'] and not saved.unread
    closed = make_item('Closed').model_copy(update={'status_category': 'done', 'reasons': []})
    store.replace_connector(result(closed), 60)
    saved = store.load()[0][0]
    assert saved.status == 'Closed' and not saved.metadata['snapshot_only']
    assert 'previously_assigned' in saved.reasons
    dashboard = build_dashboard(*store.load(), 30)
    assert len(dashboard['tracked_items']) == 1
    assert dashboard['summary']['assigned'] == 0


def test_batch_read_is_independent_and_preserves_dismissal(tmp_path):
    store = Store(tmp_path / 'test.db'); store.initialize()
    data = result(make_item()); store.replace_connector(data, 60)
    event_id = data.activities[0].id; parent_id = data.work_items[0].id
    assert store.mark_batch([event_id, event_id, 'missing'], 'unread') == 1
    assert store.load()[1][0].unread and not store.load()[0][0].unread
    undo = store.dismiss_updates(event_id)
    store.mark_batch([event_id, parent_id], 'seen')
    assert not store.load()[1]
    store.restore_dismissed(undo)
    assert not store.load()[1][0].unread
    store.mark_batch([event_id, parent_id], 'unread')
    assert store.load()[0][0].unread and store.load()[1][0].unread


def test_activity_only_discovers_durable_parent(tmp_path):
    store = Store(tmp_path / 'test.db'); store.initialize()
    data = result(make_item()).model_copy(update={'work_items': []})
    store.replace_connector(data,60)
    assert store.load()[0][0].id == data.activities[0].item_id
    assert store.load()[0][0].status == 'Unknown'

async def _exercise_history_rotation():
    from connectors.jira import JiraConnector
    from config import JiraSettings
    connector = JiraConnector(JiraSettings(),30)
    keys=[]
    def view(executable,key):
        keys.append(key)
        return {'key':key}
    connector._view=view
    records={f'ENG-{i}':{'fields':{'status':{'name':'Closed','statusCategory':{'name':'Done'}}}} for i in range(40)}
    records['ENG-ACTIVE']={'fields':{'status':{'name':'In Progress'}}}
    first, failures=await connector._hydrate_candidates('unused',records)
    second, _=await connector._hydrate_candidates('unused',records)
    assert len(first)==17 and len(second)==17 and failures
    assert 'ENG-ACTIVE' in first and 'ENG-ACTIVE' in second
    assert len(set(first)|set(second))==33


def test_closed_history_rotates_without_starving_active_work():
    import asyncio
    asyncio.run(_exercise_history_rotation())


def test_history_progress_reports_remaining_and_retries_failures():
    import asyncio
    from connectors.jira import JiraConnector
    from config import JiraSettings
    from connectors.base import ConnectorFailure
    connector = JiraConnector(JiraSettings(),30)
    records={f'ENG-{i:02d}': {'fields': {'status': {'name':'Closed','statusCategory':{'name':'Done'}}}} for i in range(35)}
    def view(executable,key):
        if key == 'ENG-00': raise ConnectorFailure('FAILED','synthetic')
        return {'key':key}
    connector._view=view
    asyncio.run(connector._hydrate_candidates('unused',records))
    assert connector._history_progress == {'total':35,'checked':15,'remaining':20,'batches_remaining':2,'failed':1}
    connector._view=lambda executable,key: {'key':key}
    asyncio.run(connector._hydrate_candidates('unused',records))
    assert connector._history_progress['remaining'] == 4
    asyncio.run(connector._hydrate_candidates('unused',records))
    assert connector._history_progress['remaining'] == 0


def test_batch_api_supports_more_than_one_thousand_rows(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app as application
    from tests.test_store import make_item, result
    store=Store(tmp_path/'batch.db'); store.initialize()
    items=[make_item().model_copy(update={'id':f'jira:issue:ENG-{i}'}) for i in range(1005)]
    store.replace_connector(result(items[0]).model_copy(update={'work_items':items,'activities':[]}),60)
    monkeypatch.setattr(application,'store',store)
    monkeypatch.setattr(application.coordinator,'store',store)
    response=TestClient(application.app).post('/api/batch-read',json={'entity_ids':[item.id for item in items],'action':'unread'})
    assert response.status_code == 200 and response.json()['changed_count'] == 1005
    assert all(item.unread for item in store.load()[0])
