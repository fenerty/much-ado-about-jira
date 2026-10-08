import json
from datetime import timedelta

import pytest

from config import AppSettings, AzureDevOpsSettings, JiraSettings, Settings, source_scope
from models import Activity, ConnectorHealth, ConnectorResult, WorkItem, utc_now
from store import Store


NOW = utc_now() - timedelta(days=1)
ACCOUNT = 'engineer@example.test'
SITE = 'https://one.atlassian.net'


def binding(connector='jira', realm=SITE, account=ACCOUNT):
    settings = Settings(
        app=AppSettings(),
        azure_devops=AzureDevOpsSettings(organization=realm, expected_account=account),
        jira=JiraSettings(site=realm, expected_account=account, activity_projects=('ENG',)),
    )
    return settings.cache_bindings[connector]


def snapshot(connector='jira', realm=SITE, account=ACCOUNT, *, reasons=None, title='Example', when=NOW):
    source = 'jira' if connector == 'jira' else 'ado'
    raw_id = 'jira:issue:ENG-1' if connector == 'jira' else 'ado:work_item:42'
    item = WorkItem(id=raw_id, source=source, source_type='issue', project='ENG',
                    key='ENG-1', title=title, url='https://example.test/item', status='Active',
                    status_category='in_progress', updated_at=when,
                    reasons=['assigned'] if reasons is None else reasons)
    event = Activity(id=raw_id + ':updated', source=source, event_type='updated',
                     item_id=raw_id, item_key=item.key, item_title=item.title, timestamp=when,
                     summary='Issue updated', url=item.url, reasons=item.reasons)
    coverage = ({'site':realm} if connector == 'jira' else
                {'organization':realm,'authenticated_identity':account})
    return ConnectorResult(connector=connector, cache_scope=source_scope(connector,realm,account),
                           work_items=[item], activities=[event],
                           health=ConnectorHealth(connector=connector,state='ok',message='Synthetic',
                                                  last_success_at=NOW,coverage=coverage))


def scoped(path, bindings):
    store = Store(path,cache_bindings=bindings)
    store.initialize()
    return store


def rows(store, table):
    with store._connect() as connection:
        return [dict(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY 1')]


def ids(store):
    work, events, _ = store.load()
    return [item.id for item in work], [event.id for event in events]


@pytest.mark.parametrize('new_realm,new_account', [
    ('https://two.atlassian.net',ACCOUNT),
    (SITE,'other@example.test'),
])
def test_same_keys_and_disjoint_rows_never_leak_relationships_or_triage(tmp_path,new_realm,new_account):
    path=tmp_path/'scopes.db'
    first=scoped(path,{'jira':binding()})
    first.replace_connector(snapshot(),60)
    old_work,old_event=ids(first)
    first.hide_work(old_work[0])
    first.dismiss_updates(old_event[0])
    with first._connect() as connection:
        original=[dict(row) for row in connection.execute('SELECT * FROM local_state ORDER BY entity_id')]
    second=scoped(path,{'jira':binding(realm=new_realm,account=new_account)})
    assert second.load() == ([],[],[])
    assert second.load_dismissed() == []
    second.replace_connector(snapshot(realm=new_realm,account=new_account,reasons=['participant'],title='Other realm/account'),60)
    work,events,_=second.load()
    assert len(work)==len(events)==1 and not work[0].unread and not events[0].unread
    assert work[0].reasons==['participant']
    assert work[0].metadata['tracked_relationships']==['participant']
    assert events[0].item_id==work[0].id and work[0].id!=old_work[0]
    assert second.load_dismissed()==[]
    assert not second.set_local_state(old_work[0],'restore')
    assert not second.set_local_state(old_event[0],'seen_related')
    assert second.mark_batch(old_work+old_event,'unread')==0
    assert second.hide_work(old_work[0])==[]
    assert second.dismiss_updates(old_event[0],True)==[]
    second.restore_dismissed([{'entity_id':old_work[0],'version':original[0]['dismissed_version']},
                              {'entity_id':old_event[0],'version':original[1]['dismissed_version']}])
    with first._connect() as connection:
        assert [dict(row) for row in connection.execute('SELECT * FROM local_state WHERE entity_id IN (?, ?) ORDER BY entity_id',(old_work[0],old_event[0]))]==original
    reopened=scoped(path,{'jira':binding()})
    assert ids(reopened)==([],[])
    assert {item['id'] for item in reopened.load_dismissed()}==set(old_work+old_event)


def test_scope_switch_preserves_independent_sources_checkpoints_baselines_and_raw_hashes(tmp_path):
    path=tmp_path/'scopes.db'
    jira_binding=binding()
    ado_binding=binding('azure_devops','org-one')
    first=scoped(path,{'jira':jira_binding,'azure_devops':ado_binding})
    jira_result=snapshot()
    first.replace_connector(jira_result,60)
    first.replace_connector(snapshot('azure_devops','org-one'),60)
    first.save_connector_state('jira',{'scope':'one','cursor':7})
    first.save_connector_state('azure_devops',{'cursor':3})
    jira_work=next(item for item in first.load()[0] if item.source=='jira')
    jira_event=next(item for item in first.load()[1] if item.source=='jira')
    first.mark_batch([jira_event.id],'unread')
    first.hide_work(jira_work.id)
    before={row['key']:row['value'] for row in rows(first,'app_meta')}
    second=scoped(path,{'jira':binding(realm='two.atlassian.net'),'azure_devops':ado_binding})
    assert [item.source for item in second.load()[0]]==['ado']
    assert second.load_connector_state('jira')=={}
    assert second.load_connector_state('azure_devops')=={'cursor':3}
    second.replace_connector(snapshot(realm='two.atlassian.net'),60)
    second.save_connector_state('jira',{'scope':'two','cursor':2})
    reopened=scoped(path,{'jira':jira_binding,'azure_devops':ado_binding})
    assert reopened.load_connector_state('jira')=={'scope':'one','cursor':7}
    assert reopened.load_connector_state('azure_devops')=={'cursor':3}
    assert next(event for event in reopened.load()[1] if event.source=='jira').unread
    assert reopened.load_dismissed()[0]['id']==jira_work.id
    after={row['key']:row['value'] for row in rows(reopened,'app_meta')}
    assert all(after[key]==value for key,value in before.items())
    for row in rows(reopened,'entities'):
        raw=json.loads(row['payload_json'])
        assert '@' not in raw['id']
        assert row['entity_id'].endswith(':'+raw['id'])
    expected=Store._payload(jira_result.work_items[0])[1]
    assert next(row for row in rows(reopened,'entities') if row['entity_id']==jira_work.id)['version_hash']==expected


def test_proven_legacy_adoption_preserves_payload_versions_read_hide_and_history(tmp_path):
    connector,realm='jira',SITE
    path=tmp_path/'legacy.db'
    legacy=Store(path);legacy.initialize()
    result=snapshot(connector,realm)
    legacy.replace_connector(result,60)
    chosen=binding(connector,realm)
    checkpoint={'scope':chosen['legacy_history_scope'],'cursor':4}
    legacy.save_connector_state(connector,checkpoint)
    legacy.hide_work(result.work_items[0].id)
    legacy.dismiss_updates(result.activities[0].id)
    originals={row['entity_id']:row for row in rows(legacy,'entities')}
    original_state={row['entity_id']:row for row in rows(legacy,'local_state')}
    store=scoped(path,{connector:chosen})
    assert store.load_connector_state(connector)==checkpoint
    assert ids(store)==([],[])
    hidden=store.load_dismissed()
    assert len(hidden)==2 and all(not entry['unread'] for entry in hidden)
    namespace=f"{connector}@{chosen['scope']}:"
    for row in rows(store,'entities'):
        raw_id=row['entity_id'].removeprefix(namespace)
        assert row['payload_json']==originals[raw_id]['payload_json']
        assert row['version_hash']==originals[raw_id]['version_hash']
        state=next(state for state in rows(store,'local_state') if state['entity_id']==row['entity_id'])
        assert state['seen_version']==original_state[raw_id]['seen_version']
        assert state['dismissed_version']==original_state[raw_id]['dismissed_version']
    assert not any(row['connector']==connector for row in rows(store,'connector_runs'))
    saved=[rows(store,table) for table in ('entities','local_state','connector_runs','app_meta')]
    store.initialize()
    assert saved==[rows(store,table) for table in ('entities','local_state','connector_runs','app_meta')]
    store.replace_connector(snapshot(connector,realm),60)
    assert len(store.load_dismissed())==2 and all(not entry['unread'] for entry in store.load_dismissed())
    assert all(json.loads(row['payload_json']).get('metadata',{}).get('tracked_relationships')==['assigned']
               for row in rows(store,'entities') if row['entity_kind']=='work_item')


@pytest.mark.parametrize('proof', ['wrong_account','wrong_realm','wrong_checkpoint','missing_checkpoint','display_name','ado_email_identity'])
def test_ambiguous_legacy_rows_are_retained_but_invisible(tmp_path,proof):
    connector='azure_devops' if proof in {'display_name','ado_email_identity'} else 'jira'
    realm='org-one' if connector=='azure_devops' else SITE
    path=tmp_path/'ambiguous.db'
    legacy=Store(path);legacy.initialize()
    old=snapshot(connector,realm)
    if proof=='display_name':
        old.health.coverage['authenticated_identity']='Engineer Example'
    legacy.replace_connector(old,60)
    expected_account='other@example.test' if proof=='wrong_account' else ACCOUNT
    expected_realm='other.atlassian.net' if proof=='wrong_realm' else realm
    chosen=binding(connector,expected_realm,expected_account)
    if proof!='missing_checkpoint':
        legacy.save_connector_state(connector,{'scope':'mismatch' if proof=='wrong_checkpoint' else binding(connector,realm)['legacy_history_scope']})
    before=[rows(legacy,table) for table in ('entities','local_state','connector_runs','app_meta')]
    store=scoped(path,{connector:chosen})
    assert store.load()==([],[],[]) and store.load_dismissed()==[]
    assert before==[rows(store,table) for table in ('entities','local_state','connector_runs','app_meta')]
    store.replace_connector(snapshot(connector,expected_realm,expected_account),60)
    assert len(store.load()[0])==1 and not store.load()[0][0].unread
    assert len([row for row in rows(store,'entities') if row['connector']==connector])==2


def test_legacy_adoption_does_not_merge_into_existing_scoped_cache(tmp_path):
    path=tmp_path/'existing.db'
    legacy=Store(path);legacy.initialize()
    legacy.replace_connector(snapshot(),60)
    chosen=binding()
    legacy.save_connector_state('jira',{'scope':chosen['legacy_history_scope']})
    store=Store(path,{'jira':chosen})
    store.replace_connector(snapshot(title='Scoped work'),60)
    before=[rows(store,table) for table in ('entities','local_state','connector_runs','app_meta')]
    store.initialize()
    assert before==[rows(store,table) for table in ('entities','local_state','connector_runs','app_meta')]
    assert [item.title for item in store.load()[0]]==['Scoped work']
    assert len([row for row in rows(store,'entities') if row['connector']=='jira'])==2


def test_unknown_account_needs_verification_and_reads_saved_checkpoint_without_activation(tmp_path,monkeypatch):
    path=tmp_path/'unverified.db'
    chosen=binding(account='')
    store=scoped(path,{'jira':chosen})
    verified=snapshot()
    with pytest.raises(ValueError,match='verified cache scope'):
        store.replace_connector(verified.model_copy(update={'cache_scope':None}),60)
    store.replace_connector(verified,60)
    store.save_connector_state('jira',{'cursor':8})
    old_work,old_events=ids(store)
    store.mark_batch(old_events,'unread')
    reopened=scoped(path,{'jira':chosen})
    assert reopened.load()==([],[],[])
    assert reopened.load_connector_state('jira')=={}
    assert reopened.load_connector_state('jira',cache_scope=verified.cache_scope)=={'cursor':8}
    assert reopened.load()==([],[],[])
    reopened.replace_connector(snapshot(),60)
    assert ids(reopened)==(old_work,old_events)
    assert reopened.load()[1][0].unread
    original_write=reopened._write_health
    def failed_write(*args):
        raise RuntimeError('Synthetic transaction failure')
    monkeypatch.setattr(reopened,'_write_health',failed_write)
    with pytest.raises(RuntimeError,match='transaction failure'):
        reopened.replace_connector(snapshot(account='other@example.test'),60)
    assert ids(reopened)==(old_work,old_events)
    assert len(rows(reopened,'entities'))==2
    monkeypatch.setattr(reopened,'_write_health',original_write)
    reopened.replace_connector(snapshot(account='other@example.test'),60)
    assert ids(reopened)!=(old_work,old_events)
    assert not reopened.set_local_state(old_events[0],'seen')
    assert reopened.load_connector_state('jira')=={}


def test_configured_account_rejects_other_verified_scope_and_checkpoint_reads(tmp_path):
    store=scoped(tmp_path/'known.db',{'jira':binding()})
    other=snapshot(account='other@example.test')
    with pytest.raises(ValueError,match='does not match'):
        store.replace_connector(other,60)
    with pytest.raises(ValueError,match='does not match'):
        store.load_connector_state('jira',cache_scope=other.cache_scope)
    with pytest.raises(ValueError,match='does not match'):
        store.activate_connector_scope('jira',other.cache_scope)
    with pytest.raises(ValueError,match='does not match'):
        store.record_health(other.health,cache_scope=other.cache_scope)
    assert store.load()==([],[],[])


def test_retention_only_prunes_the_refreshed_scope(tmp_path):
    path=tmp_path/'retention.db'
    first=scoped(path,{'jira':binding()})
    first.replace_connector(snapshot(when=NOW-timedelta(days=20)),60)
    before=rows(first,'entities')+rows(first,'local_state')
    other=scoped(path,{'jira':binding(realm='two.atlassian.net')})
    other.replace_connector(snapshot(realm='two.atlassian.net'),1)
    first_rows=[row for row in rows(first,'entities')+rows(first,'local_state') if row['entity_id'].startswith('jira@'+binding()['scope']+':')]
    assert first_rows==before


@pytest.mark.parametrize('realm,account', [('two.atlassian.net',ACCOUNT),(SITE,'other@example.test')])
def test_latest_legacy_health_cannot_adopt_mixed_source_or_account_history(tmp_path,realm,account):
    path=tmp_path/'mixed.db'
    legacy=Store(path);legacy.initialize()
    first=snapshot()
    legacy.replace_connector(first,60)
    legacy.hide_work(first.work_items[0].id)
    second=snapshot(realm=realm,account=account,title='New account/site')
    # Preserve a disjoint old row as the original cache did across omissions.
    second.work_items[0].id='jira:issue:ENG-2'
    second.work_items[0].key='ENG-2'
    second.activities[0].id='jira:issue:ENG-2:updated'
    second.activities[0].item_id='jira:issue:ENG-2'
    second.activities[0].item_key='ENG-2'
    legacy.replace_connector(second,60)
    chosen=binding(realm=realm,account=account)
    legacy.save_connector_state('jira',{'scope':chosen['legacy_history_scope'],'cursor':9})
    before=[rows(legacy,table) for table in ('entities','local_state','connector_runs','app_meta')]
    store=scoped(path,{'jira':chosen})
    assert store.load()==([],[],[]) and store.load_dismissed()==[]
    assert before==[rows(store,table) for table in ('entities','local_state','connector_runs','app_meta')]


def test_repeated_same_source_legacy_refresh_is_conservatively_quarantined(tmp_path):
    path=tmp_path/'mature.db'
    legacy=Store(path);legacy.initialize()
    legacy.replace_connector(snapshot(),60)
    legacy.replace_connector(snapshot(),60)
    chosen=binding()
    legacy.save_connector_state('jira',{'scope':chosen['legacy_history_scope']})
    before=[rows(legacy,table) for table in ('entities','local_state','connector_runs','app_meta')]
    store=scoped(path,{'jira':chosen})
    assert store.load()==([],[],[])
    assert before==[rows(store,table) for table in ('entities','local_state','connector_runs','app_meta')]
