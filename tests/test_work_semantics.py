import pytest
from tests.test_relevance import item
from relevance import build_dashboard


def test_pr_counts_distinguish_authorship_pending_review_and_completed_review():
    authored = item('pr:own', source='azure_repos', reasons=['author'])
    pending = item('pr:pending', source='azure_repos', reasons=['reviewer']).model_copy(update={'metadata': {'reviewer_vote': 0}})
    approved = item('pr:approved', source='azure_repos', reasons=['reviewer']).model_copy(update={'metadata': {'reviewer_vote': 10}})
    draft = item('pr:draft', source='azure_repos', reasons=['reviewer']).model_copy(update={'status': 'Draft', 'metadata': {'reviewer_vote': 0}})
    dashboard = build_dashboard([authored, pending, approved, draft], [], [], 30)
    assert dashboard['summary']['pr_reviews'] == 1
    assert [p['id'] for p in dashboard['code']['awaiting_review']] == ['pr:pending']
    assert {p['id'] for group in dashboard['code'].values() for p in group} == {'pr:own', 'pr:pending', 'pr:approved', 'pr:draft'}


def test_previous_assignment_is_followed_without_counting_as_current_assignment():
    previous = item('jira:past', reasons=['previously_assigned'])
    dashboard = build_dashboard([previous], [], [], 30)
    assert dashboard['summary']['assigned'] == 0
    assert [p['id'] for p in dashboard['following_waiting']] == ['jira:past']


@pytest.mark.asyncio
async def test_jira_search_includes_prior_assignment_without_project_or_time_cutoff(monkeypatch):
    from connectors.jira import JiraConnector
    from config import JiraSettings
    connector = JiraConnector(JiraSettings(activity_projects=('ENG',)), 30)
    queries = []
    def search(executable, jql):
        queries.append(jql)
        return [{'key': 'ENG-7'}] if 'assignee WAS' in jql else []
    monkeypatch.setattr(connector, '_search', search)
    records, roles, failures = await connector._query_candidates('unused', {'account_id':'example', 'email':'example@example.test'})
    assert roles['ENG-7'] == {'previously_assigned'}
    historical = next(q for q in queries if 'assignee WAS' in q)
    assert 'project IN' not in historical and 'updated >=' not in historical
    assert not failures
