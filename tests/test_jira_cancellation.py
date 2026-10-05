import asyncio
import time

import pytest

from config import JiraSettings
from connectors.base import ConnectorFailure
from connectors.jira import JiraConnector


IDENTITY = {"account_id": "synthetic-me", "email": "me@example.test", "site": "example.atlassian.net"}


def issue(key, status="In Progress"):
    return {
        "key": key,
        "fields": {
            "summary": f"Synthetic {key}",
            "status": {"name": status},
            "assignee": {"accountId": "synthetic-me", "displayName": "Me"},
            "updated": "2026-10-05T12:00:00Z",
        },
    }


def connector_with_identity(monkeypatch, **settings):
    connector = JiraConnector(JiraSettings(site="https://example.atlassian.net", **settings), 30)
    monkeypatch.setattr(connector, "_find_cli", lambda: "unused")

    async def authenticate(executable):
        return dict(IDENTITY)

    monkeypatch.setattr(connector, "_authenticate", authenticate)
    return connector


@pytest.mark.asyncio
async def test_cancel_during_history_discovery_drains_the_search(monkeypatch):
    connector = connector_with_identity(monkeypatch)
    history_started = asyncio.Event()
    history_drained = asyncio.Event()

    async def search(executable, jql, *, limit=None):
        if "statusCategory = Done" in jql:
            history_started.set()
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.sleep(0.01)
                history_drained.set()
        return []

    monkeypatch.setattr(connector, "_search", search)
    refresh = asyncio.create_task(connector.refresh())
    await asyncio.wait_for(history_started.wait(), 5)
    refresh.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(refresh, 5)
    assert history_drained.is_set()


@pytest.mark.asyncio
async def test_cancel_during_history_hydration_drains_every_view(monkeypatch):
    connector = connector_with_identity(monkeypatch)
    records = {key: issue(key, "Done") for key in ("ENG-1", "ENG-2")}
    started = set()
    finished = set()
    every_view_started = asyncio.Event()
    view_tasks = []

    async def view(executable, key):
        view_tasks.append(asyncio.current_task())
        started.add(key)
        if started == records.keys():
            every_view_started.set()
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0.01)
            finished.add(key)

    monkeypatch.setattr(connector, "_view", view)
    hydration = asyncio.create_task(connector._hydrate_candidates("unused", records))
    await asyncio.wait_for(every_view_started.wait(), 5)
    hydration.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(hydration, 5)
    assert finished == records.keys()
    assert all(task.done() for task in view_tasks)


@pytest.mark.asyncio
async def test_history_hydration_deadline_preserves_active_and_completed_views(monkeypatch):
    connector = connector_with_identity(monkeypatch, history_timeout_seconds=0.03)
    records = {"ENG-1": issue("ENG-1"), "ENG-2": issue("ENG-2", "Done"), "ENG-3": issue("ENG-3", "Done")}
    slow_drained = asyncio.Event()

    async def view(executable, key):
        if key == "ENG-3":
            try:
                await asyncio.sleep(60)
            finally:
                slow_drained.set()
        return records[key]

    monkeypatch.setattr(connector, "_view", view)
    hydrated, failures = await connector._hydrate_candidates("unused", records)

    assert set(hydrated) == {"ENG-1", "ENG-2"}
    assert "view:ENG-3" in failures
    assert slow_drained.is_set()


@pytest.mark.asyncio
async def test_history_discovery_cannot_exhaust_active_hydration_budget(monkeypatch):
    connector = connector_with_identity(monkeypatch)
    active = issue("ENG-1")

    async def search(executable, jql, *, limit=None):
        if "statusCategory = Done" in jql:
            connector._deadline = time.monotonic() - 1
            raise ConnectorFailure("JIRA_TIMEOUT", "Synthetic history exhausted the refresh budget")
        if jql.startswith("assignee = currentUser()"):
            return [active]
        return []

    async def view(executable, key):
        if connector._deadline <= time.monotonic():
            raise ConnectorFailure("JIRA_TIMEOUT", "Synthetic refresh budget exhausted")
        return active

    monkeypatch.setattr(connector, "_search", search)
    monkeypatch.setattr(connector, "_view", view)
    result = await connector.refresh()

    assert result.health.state == "partial"
    assert [item.key for item in result.work_items] == ["ENG-1"]
    assert any("completed:JIRA_TIMEOUT" in path for path in result.health.coverage["unavailable_queries"])


@pytest.mark.asyncio
async def test_queued_command_checks_budget_after_acquiring_its_slot(monkeypatch):
    connector = connector_with_identity(monkeypatch)
    connector._deadline = time.monotonic() + 60
    connector._command_slots = asyncio.Semaphore(1)
    await connector._command_slots.acquire()
    called = False

    async def command(*args):
        nonlocal called
        called = True
        raise AssertionError("An expired queued command must not start")

    monkeypatch.setattr("connectors.jira.async_run_command", command)
    queued = asyncio.create_task(connector._run("unused", ["jira", "auth", "status"]))
    await asyncio.sleep(0)
    connector._deadline = time.monotonic() - 1
    connector._command_slots.release()
    with pytest.raises(ConnectorFailure) as failure:
        await queued

    assert failure.value.code == "JIRA_TIMEOUT"
    assert called is False
