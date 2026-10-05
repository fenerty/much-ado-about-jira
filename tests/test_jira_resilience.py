import asyncio
import json
import subprocess
import time
from dataclasses import replace

import pytest

import connectors.jira as jira_module
import refresh as refresh_module
from config import AppSettings, AzureDevOpsSettings, JiraSettings, Settings
from connectors.base import ConnectorFailure
from connectors.jira import JiraConnector
from connectors.jira_history import HISTORY_ROLES, JiraHistory
from models import ConnectorHealth, ConnectorResult, utc_now
from refresh import RefreshCoordinator
from store import Store


IDENTITY = {"site": "example.atlassian.net", "email": "me@example.test", "account_id": "self", "display_name": "Me"}


def issue(key, *, done=False, assignee="self"):
    return {"key": key, "fields": {
        "summary": f"Synthetic {key}",
        "status": {"name": "Done" if done else "In Progress", "statusCategory": {"name": "Done" if done else "In Progress"}},
        "assignee": {"accountId": assignee, "displayName": assignee},
        "updated": utc_now().isoformat(), "comment": {"comments": [], "total": 0},
    }}


def connector(**settings):
    return JiraConnector(JiraSettings(site="https://example.atlassian.net", expected_account="me@example.test", **settings), 30)


def wire_refresh(monkeypatch, target, search, view=None):
    async def authenticate(executable):
        return dict(IDENTITY)

    async def default_view(executable, key):
        return issue(key)

    monkeypatch.setattr(target, "_find_cli", lambda: "synthetic-acli")
    monkeypatch.setattr(target, "_authenticate", authenticate)
    monkeypatch.setattr(target, "_search", search)
    monkeypatch.setattr(target, "_view", view or default_view)


def test_async_runner_timeout_is_translated_to_jira_failure(monkeypatch):
    target = connector()

    async def timed_out(executable, args, timeout):
        raise subprocess.TimeoutExpired([executable, *args], timeout)

    monkeypatch.setattr(jira_module, "async_run_command", timed_out)
    with pytest.raises(ConnectorFailure) as error:
        asyncio.run(target._run("synthetic-acli", ["jira", "workitem", "search", "--jql", "key = ENG-1", "--json"]))
    assert error.value.code == "JIRA_TIMEOUT"


def test_completed_discovery_exhaustion_preserves_already_hydrated_active_work(monkeypatch):
    target = connector()
    target._ensure_history(IDENTITY)
    target._history.cursors["assigned"] = "ENG-99"
    target.load_state(target.checkpoint())
    observed = []

    async def search(executable, jql, *, limit=None):
        if "statusCategory = Done" in jql:
            assert observed == ["active-view"]
            target._deadline = time.monotonic() - 1
            raise ConnectorFailure("JIRA_TIMEOUT", "Synthetic history deadline")
        return [issue("ENG-1")] if jql.startswith("assignee =") else []

    async def view(executable, key):
        assert target._deadline > time.monotonic()
        observed.append("active-view")
        return issue(key)

    wire_refresh(monkeypatch, target, search, view)
    result = asyncio.run(target.refresh())
    assert result.health.state == "partial"
    assert [item.key for item in result.work_items] == ["ENG-1"]
    assert result.work_items[0].reasons == ["assigned"]
    assert result.health.last_success_at is not None
    assert "assigned:completed:JIRA_TIMEOUT" in result.health.coverage["unavailable_queries"]
    assert target.checkpoint()["cursors"]["assigned"] == "ENG-99"


def test_late_comment_timeout_retains_active_item_and_reports_partial(monkeypatch):
    target = connector(activity_projects=("ENG",))

    async def search(executable, jql, *, limit=None):
        return [issue("ENG-1")] if jql.startswith("assignee =") or "updatedBy(" in jql else []

    async def comments(executable, key):
        raise ConnectorFailure("JIRA_TIMEOUT", "Synthetic comments deadline")

    wire_refresh(monkeypatch, target, search)
    monkeypatch.setattr(target, "_comment_list", comments)
    result = asyncio.run(target.refresh())
    assert result.health.state == "partial"
    assert [item.key for item in result.work_items] == ["ENG-1"]
    assert "assigned" in result.work_items[0].reasons
    assert "comments:ENG-1:JIRA_TIMEOUT" in result.health.coverage["unavailable_queries"]


def test_history_phase_timeout_keeps_completed_views_and_cancels_pending_reads(monkeypatch):
    target = connector(history_timeout_seconds=0.01)
    target._ensure_history(IDENTITY)
    target._history.records = {"ENG-2": {"assigned"}, "ENG-3": {"assigned"}}
    target.load_state(target.checkpoint())
    canceled = []

    async def search(executable, jql, *, limit=None):
        return [issue("ENG-1")] if jql.startswith("assignee =") else []

    async def view(executable, key):
        if key == "ENG-3":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                canceled.append(key)
                raise
        return issue(key, done=key != "ENG-1")

    wire_refresh(monkeypatch, target, search, view)
    result = asyncio.run(target.refresh())
    assert result.health.state == "partial"
    assert {item.key for item in result.work_items} == {"ENG-1", "ENG-2"}
    assert canceled == ["ENG-3"]
    assert target.checkpoint()["checked"] == ["ENG-2"]
    assert "view:ENG-3" in result.health.coverage["unavailable_queries"]


def test_hung_discovery_is_canceled_without_advancing_source_cursor(monkeypatch):
    target = connector(history_timeout_seconds=0.01)
    target._ensure_history(IDENTITY)
    target._history.cursors["assigned"] = "ENG-10"
    canceled = []

    async def search(executable, jql, *, limit=None):
        if "statusCategory = Done" in jql:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                canceled.append(jql)
                raise
        return []

    monkeypatch.setattr(target, "_search", search)
    _, _, failures = asyncio.run(target._query_candidates("synthetic-acli", dict(IDENTITY)))
    assert len(canceled) == 1
    assert target.checkpoint()["cursors"]["assigned"] == "ENG-10"
    assert any(value.startswith("assigned:completed:") for value in failures)


def test_search_uses_source_limits_without_automatic_pagination(monkeypatch):
    target = connector(max_candidates_per_query=7)
    commands = []

    async def run(executable, args):
        commands.append(args)
        return json.dumps([issue("ENG-1")])

    monkeypatch.setattr(target, "_run", run)
    asyncio.run(target._search("synthetic-acli", "assignee = currentUser()"))
    asyncio.run(target._search("synthetic-acli", "statusCategory = Done", limit=2))
    assert [args[args.index("--limit") + 1] for args in commands] == ["7", "2"]
    assert all("--paginate" not in args for args in commands)


def test_discovery_uses_jira_last_row_order_rotates_roles_and_resets_completed_pass(monkeypatch):
    target = connector(history_batch_size=2)
    history_queries = []

    async def search(executable, jql, *, limit=None):
        if "statusCategory = Done" not in jql:
            return []
        history_queries.append(jql)
        if jql.startswith("(assignee =") and "key >" not in jql:
            return [issue("ENG-9", done=True), issue("ENG-10", done=True)]
        if jql.startswith("(assignee WAS"):
            return [issue("ENG-20", done=True)]
        return []

    monkeypatch.setattr(target, "_search", search)
    asyncio.run(target._query_candidates("synthetic-acli", dict(IDENTITY)))
    assert target.checkpoint()["cursors"]["assigned"] == "ENG-10"
    assert target.checkpoint()["passes"]["assigned"] == 0
    for _ in range(4):
        asyncio.run(target._query_candidates("synthetic-acli", dict(IDENTITY)))
    state = target.checkpoint()
    assert all(state["passes"][role] == 1 for role in HISTORY_ROLES)
    assert state["cursors"]["assigned"] == ""
    assert state["next_role"] == 1
    assert 'key > "ENG-10"' in history_queries[-1]
    assert state["records"]["ENG-9"] == ["assigned"]
    assert state["records"]["ENG-20"] == ["previously_assigned"]


def test_hydration_checkpoint_reopens_and_advances_without_repeating_successes(tmp_path, monkeypatch):
    store = Store(tmp_path / "isolated.sqlite3")
    store.initialize()
    target = connector()
    target._ensure_history(IDENTITY)
    target._history.records = {f"ENG-{index:02d}": {"assigned"} for index in range(40)}
    records = {key: issue(key, done=True) for key in target._history.records}

    async def view(executable, key):
        return issue(key, done=True)

    monkeypatch.setattr(target, "_view", view)
    first, _ = asyncio.run(target._hydrate_candidates("synthetic-acli", records))
    store.save_connector_state("jira", target.checkpoint())
    reopened = Store(store.database_path)
    second_target = connector()
    second_target.load_state(reopened.load_connector_state("jira"))
    second_target._ensure_history(IDENTITY)
    monkeypatch.setattr(second_target, "_view", view)
    second, _ = asyncio.run(second_target._hydrate_candidates("synthetic-acli", records))
    assert len(first) == len(second) == 16
    assert not set(first) & set(second)
    assert len(second_target.checkpoint()["checked"]) == 32
    assert second_target._history_progress["remaining"] == 8


def test_failed_history_key_is_retried_without_starving_later_keys(monkeypatch):
    target = connector()
    target._ensure_history(IDENTITY)
    target._history.records = {f"ENG-{index:02d}": {"assigned"} for index in range(35)}
    records = {key: issue(key, done=True) for key in target._history.records}
    attempts = []

    async def view(executable, key):
        attempts.append(key)
        if key == "ENG-00":
            raise ConnectorFailure("JIRA_TIMEOUT", "Synthetic persistent failure")
        return issue(key, done=True)

    monkeypatch.setattr(target, "_view", view)
    for _ in range(3):
        asyncio.run(target._hydrate_candidates("synthetic-acli", records))
    assert target._history_progress["checked"] == 34
    assert target._history_progress["remaining"] == 1
    assert "ENG-34" in attempts
    assert attempts.count("ENG-00") == 2
    assert "ENG-00" not in target.checkpoint()["checked"]


def test_stale_history_assignment_and_watching_are_not_current_relationships():
    target = connector()
    target._ensure_history(IDENTITY)
    target._history.records["ENG-1"] = {"assigned", "watching"}
    items, _, _ = asyncio.run(target._normalize(
        "synthetic-acli", {"ENG-1": issue("ENG-1", assignee="other")},
        {"ENG-1": {"assigned", "watching"}}, dict(IDENTITY),
    ))
    assert items[0].status_category == "in_progress"
    assert items[0].reasons == ["previously_assigned"]
    assert items[0].metadata["tracked_relationships"] == ["assigned", "watching"]


@pytest.mark.parametrize("change", ["site", "email", "activity_projects", "participation_days", "mention_reply_days"])
def test_account_site_and_configuration_changes_discard_old_history(change):
    original = connector()
    original._ensure_history(IDENTITY)
    original._history.records["ENG-1"] = {"assigned"}
    original._history.cursors["assigned"] = "ENG-10"
    saved = original.checkpoint()
    new_identity = dict(IDENTITY)
    settings = original.settings
    if change in {"site", "email"}:
        new_identity[change] = "another.atlassian.net" if change == "site" else "another@example.test"
    else:
        settings = replace(settings, **{change: ("OTHER",) if change == "activity_projects" else 123})
    new = JiraConnector(settings, 30)
    new.load_state(saved)
    new._ensure_history(new_identity)
    assert new.checkpoint()["scope"] != saved["scope"]
    assert new.checkpoint()["records"] == {}
    assert all(cursor == "" for cursor in new.checkpoint()["cursors"].values())


def test_malformed_nested_history_state_recovers_valid_subset():
    state = {"version": 1, "scope": "same", "records": {"ENG-1": ["assigned", {}, "invalid"], "ENG-2": "assigned"},
             "cursors": [], "passes": {"assigned": -1, "watching": "bad"}, "checked": 42,
             "next_role": "bad", "hydration_cursor": []}
    history = JiraHistory("same", state)
    assert history.records == {"ENG-1": {"assigned"}}
    assert history.checked == set()
    assert all(value == 0 for value in history.passes.values())
    assert history.next_role == 0
    assert history.hydration_cursor == ""
    assert JiraHistory("same", {"version": 999, "scope": "same", "records": state["records"]}).records == {}


def test_coordinator_uses_independent_jira_deadline_and_saves_checkpoint_after_snapshot(tmp_path, monkeypatch):
    settings = Settings(AppSettings(connector_timeout_seconds=1), AzureDevOpsSettings(),
                        JiraSettings(refresh_timeout_seconds=99), state_dir=tmp_path)
    events = []

    class RecordingStore(Store):
        def replace_connector(self, result, retention_days):
            super().replace_connector(result, retention_days)
            events.append(("snapshot", result.connector))

        def save_connector_state(self, name, state):
            assert ("snapshot", name) in events
            super().save_connector_state(name, state)
            events.append(("checkpoint", name))

    store = RecordingStore(tmp_path / "isolated.sqlite3")
    store.initialize()
    saved_state = {"scope": "existing", "version": 1}
    Store.save_connector_state(store, "jira", saved_state)
    coordinator = RefreshCoordinator(settings, store)
    jira = coordinator.connectors[1]
    deadlines = []

    async def fake_wait_for(awaitable, timeout):
        deadlines.append(timeout)
        return await awaitable

    async def jira_refresh():
        assert jira._saved_state == saved_state
        return ConnectorResult(connector="jira", health=ConnectorHealth(connector="jira", state="partial", message="Synthetic"))

    async def ado_refresh():
        return ConnectorResult(connector="azure_devops", health=ConnectorHealth(connector="azure_devops", state="ok", message="Synthetic"))

    monkeypatch.setattr(refresh_module.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(jira, "refresh", jira_refresh)
    monkeypatch.setattr(jira, "checkpoint", lambda: {"scope": "new", "version": 1, "cursor": 2})
    monkeypatch.setattr(coordinator.connectors[0], "refresh", ado_refresh)
    asyncio.run(coordinator.refresh())
    assert sorted(deadlines) == [6, 104]
    assert events.index(("snapshot", "jira")) < events.index(("checkpoint", "jira"))
    assert Store(store.database_path).load_connector_state("jira")["cursor"] == 2


def test_coordinator_does_not_advance_checkpoint_when_snapshot_write_fails(tmp_path, monkeypatch):
    settings = Settings(AppSettings(), AzureDevOpsSettings(), JiraSettings(), state_dir=tmp_path)
    store = Store(tmp_path / "isolated.sqlite3")
    store.initialize()
    store.save_connector_state("jira", {"cursor": "old"})
    coordinator = RefreshCoordinator(settings, store)
    jira = coordinator.connectors[1]
    coordinator.connectors = [jira]

    async def refresh():
        return ConnectorResult(connector="jira", health=ConnectorHealth(connector="jira", state="partial", message="Synthetic"))

    def fail_snapshot(result, retention):
        raise RuntimeError("Synthetic snapshot failure")

    monkeypatch.setattr(jira, "refresh", refresh)
    monkeypatch.setattr(jira, "checkpoint", lambda: {"cursor": "new"})
    monkeypatch.setattr(store, "replace_connector", fail_snapshot)
    with pytest.raises(RuntimeError, match="snapshot failure"):
        asyncio.run(coordinator.refresh())
    assert store.load_connector_state("jira") == {"cursor": "old"}
