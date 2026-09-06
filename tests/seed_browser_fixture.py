"""Populate the ignored browser-QA cache with synthetic, non-corporate records."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from models import Activity, ConnectorHealth, ConnectorResult, WorkItem
from store import Store


now = datetime.now(timezone.utc)
settings = load_settings()
# Always isolate fixtures from the real user cache, regardless of configuration.
store = Store(Path(__file__).resolve().parents[1] / ".runtime" / "browser-qa.sqlite3")
store.initialize()
# Reset only the isolated synthetic fixture so repeated QA starts from the same baseline.
with store._connect() as connection:
    for table in ("entities", "local_state", "connector_runs", "app_meta"):
        connection.execute(f"DELETE FROM {table}")


def issue(source: str, number: int, reasons: list[str], *, blocked: bool = False) -> WorkItem:
    prefix = "ENG" if source == "jira" else ""
    key = f"{prefix}-{130000 + number}" if source == "jira" else str(18000 + number)
    return WorkItem(
        id=f"{source}:workitem:{key}",
        source=source,
        source_type="issue" if source == "jira" else "user_story",
        project="Platform" if source == "jira" else "Engineering",
        key=key,
        title=["Add request tracing to the deployment pipeline", "Resolve intermittent timeout in the events API", "Document the service ownership handoff", "Improve error messages for failed imports", "Add retry handling to the notification worker", "Review access controls for the developer portal"][(number - 1) % 6],
        url="https://example.invalid/item",
        status="Blocked" if blocked else ("In Progress" if number % 3 else "Ready"),
        status_category="blocked" if blocked else ("in_progress" if number % 3 else "todo"),
        priority=str((number % 4) + 1),
        assigned_to="Example Engineer" if "assigned" in reasons else "Teammate",
        author="Example Engineer" if "author" in reasons else "Teammate",
        updated_at=now - timedelta(hours=number * 3),
        actionable=blocked,
        reasons=reasons,
    )


jira_items = [issue("jira", index, ["assigned"], blocked=index == 2) for index in range(1, 11)]
jira_items.extend(issue("jira", index, ["watching", "participant"]) for index in range(11, 16))
ado_items = [issue("ado", index, ["assigned"]) for index in range(16, 24)]
pr_items = [
    WorkItem(
        id=f"azure_repos:pr:fixture:{index}",
        source="azure_repos",
        source_type="pull_request",
        project="Engineering",
        repository="platform-api",
        key=f"PR #{6800 + index}",
        title=["Add pagination to the audit events endpoint", "Fix race condition in background job scheduling", "Reduce cold-start time for the API service", "Refactor configuration loading for local development", "Update integration test fixtures"][(index - 1) % 5],
        url="https://example.invalid/pr",
        status="Draft" if index == 4 else "Active",
        status_category="in_progress",
        author="Example Engineer" if index > 2 else "Teammate",
        assigned_to="Example Engineer" if index <= 2 else None,
        updated_at=now - timedelta(hours=index),
        actionable=index <= 2,
        reasons=["reviewer"] if index <= 2 else ["author", "waiting"],
        metadata={"unresolved_threads": index % 2, "failed_checks": int(index == 3)},
    )
    for index in range(1, 6)
]


def activities(items: list[WorkItem], source: str) -> list[Activity]:
    result = []
    for index, item in enumerate(items[:12], 1):
        result.append(
            Activity(
                id=f"{source}:fixture:activity:{index}",
                source=item.source,
                event_type="updated",
                actor="Teammate",
                item_id=item.id,
                item_key=item.key,
                item_title=item.title,
                timestamp=now - timedelta(minutes=index * 11),
                summary="Updated the implementation notes",
                url=item.url,
                reasons=item.reasons,
            )
        )
    return result


store.replace_connector(
    ConnectorResult(
        connector="jira",
        work_items=jira_items,
        activities=activities(jira_items, "jira"),
        health=ConnectorHealth(
            connector="jira", state="partial", message="Demo: comment discovery is limited to configured projects", last_success_at=now
        ),
    ),
    60,
)
store.replace_connector(
    ConnectorResult(
        connector="azure_devops",
        work_items=ado_items + pr_items,
        activities=activities(ado_items + pr_items, "azure_devops"),
        health=ConnectorHealth(
            connector="azure_devops", state="ok", message="Demo: synthetic source snapshot", last_success_at=now
        ),
    ),
    60,
)

jira_items[0] = jira_items[0].model_copy(
    update={
        "status": "Blocked",
        "status_category": "blocked",
        "updated_at": now + timedelta(seconds=1),
        "reasons": ["assigned", "mentioned"],
    }
)
new_activity = Activity(
    id="jira:fixture:activity:new-mention",
    source="jira",
    event_type="mention",
    actor="Teammate",
    item_id=jira_items[0].id,
    item_key=jira_items[0].key,
    item_title=jira_items[0].title,
    timestamp=now + timedelta(seconds=1),
    summary="Asked for your input on the proposed fix",
    url=jira_items[0].url,
    reasons=["mentioned"],
)
store.replace_connector(
    ConnectorResult(
        connector="jira",
        work_items=jira_items,
        activities=activities(jira_items, "jira") + [new_activity],
        health=ConnectorHealth(
            connector="jira", state="partial", message="Demo: comment discovery is limited to configured projects", last_success_at=now
        ),
    ),
    60,
)
