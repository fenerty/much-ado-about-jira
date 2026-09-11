from datetime import datetime, timedelta, timezone

from models import Activity, ConnectorHealth, ConnectorResult, WorkItem
from store import Store


NOW = datetime.now(timezone.utc) - timedelta(days=1)


def make_item(status="Active", updated=NOW):
    return WorkItem(
        id="jira:issue:ENG-1",
        source="jira",
        source_type="issue",
        project="ENG",
        key="ENG-1",
        title="Investigate discrepancy",
        url="https://example.atlassian.net/browse/ENG-1",
        status=status,
        status_category="in_progress",
        updated_at=updated,
        actionable=True,
        reasons=["assigned"],
    )


def result(item):
    return ConnectorResult(
        connector="jira",
        work_items=[item],
        activities=[
            Activity(
                id=f"jira:issue:ENG-1:updated:{item.updated_at.isoformat()}",
                source="jira",
                event_type="updated",
                item_id=item.id,
                item_key=item.key,
                item_title=item.title,
                timestamp=item.updated_at,
                summary="Issue updated",
                url=item.url,
                reasons=item.reasons,
            )
        ],
        health=ConnectorHealth(connector="jira", state="ok", message="ok"),
    )


def test_baseline_seen_then_change_unread_and_hidden_work_requires_restore(tmp_path):
    store = Store(tmp_path / "dashboard.sqlite3")
    store.initialize()
    first = make_item()
    store.replace_connector(result(first), 60)

    work, activity, _ = store.load()
    assert work[0].unread is False
    assert activity[0].unread is False

    changed = make_item("Blocked", NOW + timedelta(hours=1))
    store.replace_connector(result(changed), 60)
    work, _, _ = store.load()
    assert work[0].unread is True

    assert store.set_local_state(changed.id, "seen")
    assert store.load()[0][0].unread is False
    assert store.set_local_state(changed.id, "dismiss")
    assert store.load()[0] == []

    changed_again = make_item("In Progress", NOW + timedelta(hours=2))
    store.replace_connector(result(changed_again), 60)
    work, _, _ = store.load()
    assert work == []
    assert store.set_local_state(changed_again.id, 'restore')
    work, _, _ = store.load()
    assert work[0].unread is True


def test_failed_health_update_does_not_require_snapshot_replacement(tmp_path):
    store = Store(tmp_path / "dashboard.sqlite3")
    store.initialize()
    store.replace_connector(result(make_item()), 60)
    store.record_health(
        ConnectorHealth(connector="jira", state="auth_required", message="login")
    )
    work, _, health = store.load()
    assert len(work) == 1
    assert health[0].state == "auth_required"
