import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest
from connectors.jira import JiraConnector
from config import JiraSettings
from connectors.base import completed_within


@pytest.mark.asyncio
async def test_active_jira_assignments_have_budget_separate_from_completed(monkeypatch):
    connector=JiraConnector(JiraSettings(max_candidates_per_query=2),30)
    queries=[]
    def search(executable,jql):
        queries.append(jql)
        if jql.startswith('assignee = currentUser()'):
            if 'statusCategory != Done' in jql: return [{'key':'ENG-OLD-ACTIVE'}]
            return [{'key':f'ENG-DONE-{i}'} for i in range(5)]
        return []
    monkeypatch.setattr(connector,'_search',search)
    records, roles, failures=await connector._query_candidates('unused',{'account_id':'synthetic'})
    assert 'ENG-OLD-ACTIVE' in records and len(records)==3
    assert roles['ENG-OLD-ACTIVE']=={'assigned'}
    assert 'assigned:completed:safety_limit' in failures
    assert not any('project IN ()' in query for query in queries)
    assert not any('participant' in failure or 'mention' in failure for failure in failures)
    for role in ('assignee WAS','watcher =','creator ='):
        assert any(role in query and 'statusCategory != Done' in query for query in queries)


@pytest.mark.asyncio
async def test_history_deadline_keeps_completed_and_cancels_pending():
    canceled=asyncio.Event()
    async def quick(): return ('item',['Status changed'],'Person')
    async def slow():
        try: await asyncio.sleep(30)
        finally: canceled.set()
    results=await completed_within([quick(),slow()],timeout=.02)
    assert results==[('item',['Status changed'],'Person')]
    assert canceled.is_set()


def test_undo_accepts_every_version_from_large_related_dismissal(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    import app as application
    from store import Store
    from tests.test_store import make_item, result
    data=result(make_item())
    data.activities=[data.activities[0].model_copy(update={'id':f'event:{i}'}) for i in range(1005)]
    store=Store(tmp_path/'undo.db');store.initialize();store.replace_connector(data,60)
    monkeypatch.setattr(application,'store',store);monkeypatch.setattr(application.coordinator,'store',store)
    client=TestClient(application.app)
    dismissed=client.post('/api/local-state',json={'entity_id':'event:0','action':'dismiss_related'}).json()
    assert len(dismissed['undo_entries'])==1005
    response=client.post('/api/undo-dismiss',json={'entries':dismissed['undo_entries']})
    assert response.status_code==200 and len(response.json()['activity'])==1005


def test_listener_probe_distinguishes_app_from_other_service():
    from startup import listener_identity
    class Handler(BaseHTTPRequestHandler):
        payload={'unrelated':True}
        def do_GET(self):
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(self.payload).encode())
        def log_message(self,*args): pass
    server=HTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        assert listener_identity('127.0.0.1',server.server_port)=='other'
        Handler.payload={'application':'much-ado-about-jira'}
        assert listener_identity('127.0.0.1',server.server_port)=='ours'
    finally: server.shutdown();server.server_close();thread.join()
    assert listener_identity('127.0.0.1',server.server_port)=='none'


def test_port_collision_never_opens_other_service(monkeypatch):
    import app as application
    monkeypatch.setattr(application,'acquire_instance',lambda port: True)
    monkeypatch.setattr(application,'listener_identity',lambda host,port: 'other')
    monkeypatch.setattr('sys.argv',['app.py','--background'])
    with patch('webbrowser.open') as opened, pytest.raises(SystemExit) as error:
        application.main()
    assert error.value.code==1
    opened.assert_not_called()
