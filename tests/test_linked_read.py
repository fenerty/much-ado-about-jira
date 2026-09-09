from datetime import timedelta
from store import Store
from tests.test_store import make_item, result, NOW


def populated(tmp_path):
    store=Store(tmp_path/'read.db');store.initialize()
    parent=make_item()
    data=result(parent)
    data.activities=[data.activities[0].model_copy(update={'id':f'event:{i}'}) for i in range(3)]
    other=parent.model_copy(update={'id':'jira:issue:OTHER'})
    data.work_items.append(other)
    data.activities.append(data.activities[0].model_copy(update={'id':'other:event','item_id':other.id}))
    store.replace_connector(data,60)
    store.mark_batch([item.id for item in data.work_items+data.activities],'unread')
    return store,parent,data


def states(store):
    work,events,_=store.load()
    return {item.id:item.unread for item in work+events}


def test_read_update_reads_parent_but_not_siblings_or_other_work(tmp_path):
    store,parent,data=populated(tmp_path)
    store.set_local_state('event:0','seen')
    after=states(store)
    assert not after['event:0'] and not after[parent.id]
    assert after['event:1'] and after['event:2'] and after['other:event'] and after['jira:issue:OTHER']
    assert states(Store(store.database_path))==after


def test_read_work_reads_all_current_events_and_preserves_future_unread(tmp_path):
    store,parent,data=populated(tmp_path)
    store.set_local_state(parent.id,'seen')
    after=states(store)
    assert all(not after[f'event:{i}'] for i in range(3))
    assert not after[parent.id] and after['other:event']
    updated=parent.model_copy(update={'status':'Blocked','updated_at':NOW+timedelta(hours=1)})
    store.replace_connector(result(updated),60)
    after=states(store)
    assert after[updated.id] and after[result(updated).activities[0].id]


def test_mixed_batch_uses_original_selection_and_preserves_dismissals(tmp_path):
    store,parent,data=populated(tmp_path)
    undo=store.dismiss_updates('event:2')
    store.mark_batch(['event:0','jira:issue:OTHER'],'seen')
    after=states(store)
    assert not after[parent.id] and not after['event:0'] and after['event:1']
    assert not after['other:event'] and not after['jira:issue:OTHER']
    assert len(store.load_dismissed())==1
    store.mark_batch([parent.id],'seen')
    assert len(store.load_dismissed())==1
    store.restore_dismissed(undo)
    assert not states(store)['event:2']
    store.mark_batch([parent.id],'unread')
    assert states(store)[parent.id] and not states(store)['event:0']
