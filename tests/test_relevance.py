from datetime import datetime, timedelta, timezone

from models import Activity, ConnectorHealth, WorkItem
from relevance import build_dashboard


NOW = datetime.now(timezone.utc)


def item(item_id, source="jira", reasons=None, actionable=False, category="todo", days_old=0):
    return WorkItem(
        id=item_id,
        source=source,
        source_type="pull_request" if source == "azure_repos" else "issue",
        project="ENG",
        repository="repo" if source == "azure_repos" else None,
        key="ENG-1" if source == "jira" else "PR #7",
        title="A useful title",
        url="https://example.test/item",
        status="Active",
        status_category=category,
        updated_at=NOW - timedelta(days=days_old),
        actionable=actionable,
        reasons=reasons or [],
    )


def test_dashboard_groups_and_counts():
    assigned = item("jira:1", reasons=["assigned"], actionable=False, category="in_progress", days_old=31)
    review = item("azure_repos:pr:7", source="azure_repos", reasons=["reviewer"], actionable=True)
    waiting = item("jira:2", reasons=["author", "waiting"])
    mention = Activity(
        id="jira:activity:1",
        source="jira",
        event_type="mention",
        item_id=assigned.id,
        item_key=assigned.key,
        item_title=assigned.title,
        timestamp=NOW,
        summary="You were mentioned",
        url=assigned.url,
        reasons=["mentioned"],
        unread=True,
    )
    dashboard = build_dashboard(
        [assigned, review, waiting],
        [mention],
        [ConnectorHealth(connector="jira", state="ok", message="ok")],
        stale_after_days=30,
    )
    assert dashboard["summary"] == {
        "assigned": 1,
        "pr_reviews": 1,
        "new_mentions_replies": 1,
        "following_waiting": 1,
        "stale": 1,
    }
    assert len(dashboard["my_work"]["in_progress"]) == 1
    assert len(dashboard["code"]["awaiting_review"]) == 1
    assert dashboard["following_waiting"][0]["id"] == waiting.id
    assert dashboard["needs_attention"][0]["entity_kind"] == "activity"


def test_routine_seen_assignment_is_not_duplicated_into_attention():
    routine = item("jira:routine", reasons=["assigned"], actionable=False)
    dashboard = build_dashboard([routine], [], [], stale_after_days=30)
    assert len(dashboard["my_work"]["todo"]) == 1
    assert dashboard["needs_attention"] == []


def test_attention_caps_stale_cleanup_candidates():
    stale = [
        item(f"jira:stale:{index}", reasons=["assigned"], days_old=31 + index)
        for index in range(8)
    ]
    dashboard = build_dashboard(stale, [], [], stale_after_days=30)
    assert len(dashboard["needs_attention"]) == 5
    assert dashboard["summary"]["stale"] == 8
