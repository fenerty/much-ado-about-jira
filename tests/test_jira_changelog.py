import asyncio
import json
import time
from datetime import timedelta

import httpx
import pytest

import connectors.jira as jira_module
from config import JiraSettings, source_scope
from connectors.jira import JiraConnector
from connectors.jira_changelog import (
    JiraChangelogHistory, changelog_activity, jira_changes, parse_changelog_page,
)
from models import Activity, WorkItem, utc_now


NOW = utc_now() - timedelta(hours=1)
IDENTITY = {"site": "example.atlassian.net", "email": "me@example.test", "account_id": "self"}
SCOPE = source_scope("jira", IDENTITY["site"], IDENTITY["email"])


def item(key="ENG-1", when=NOW):
    return WorkItem(id=f"jira:issue:{key}", source="jira", source_type="issue", project="ENG",
                    key=key, title=f"Synthetic {key}", url=f"https://example.atlassian.net/browse/{key}",
                    status="Open", updated_at=when, reasons=["assigned"])


def target(**settings):
    connector = JiraConnector(JiraSettings(site="https://example.atlassian.net", expected_account="me@example.test",
                                          changelog_enabled=True, **settings), 30)
    connector.verified_cache_scope = SCOPE
    return connector


def change(identifier="10001", *, when=NOW, fields=None):
    return {"id": identifier, "created": when.isoformat(),
            "author": {"displayName": "Engineer <secret@example.test>", "accountId": "private-id"},
            "items": fields if fields is not None else [
                {"field": "status", "fieldId": "status", "fromString": "Open", "toString": "In Progress"}]}


def page(rows, offset=0, *, last=True, total=None):
    return {"startAt": offset, "maxResults": 100, "total": total if total is not None else offset + len(rows),
            "isLast": last, "values": rows, "nextPage": "https://evil.example/steal"}


def transport(monkeypatch, handler):
    monkeypatch.setenv("JIRA_API_TOKEN", "synthetic-token-never-cache")
    real_client = httpx.AsyncClient
    requests = []

    async def handle(request):
        requests.append(request)
        if request.url.path == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "self", "emailAddress": IDENTITY["email"]})
        return handler(request)

    monkeypatch.setattr(jira_module.httpx, "AsyncClient", lambda **kwargs:
                        real_client(transport=httpx.MockTransport(handle), **kwargs))
    return requests


def test_paginated_capture_resumes_after_restart_and_rechecks_last_row_without_duplicates(monkeypatch):
    offsets = []

    def handler(request):
        offset = int(request.url.params["startAt"])
        offsets.append(offset)
        rows = [change("a"), change("b")] if offset == 0 else [change("c")]
        return httpx.Response(200, json=page(rows, offset, last=offset != 0, total=3))

    requests = transport(monkeypatch, handler)
    first = target(changelog_page_size=2)
    events = asyncio.run(first._collect_changelog([item()], IDENTITY))
    assert [event.source_update_id for event in events] == ["a", "b"]
    assert first._changelog.records["ENG-1"]["offset"] == 2
    assert first._changelog_coverage["state"] == "updating"
    restarted = target(changelog_page_size=2)
    restarted.load_state(first.checkpoint())
    events = asyncio.run(restarted._collect_changelog([item()], IDENTITY))
    assert [event.source_update_id for event in events] == ["c"]
    assert restarted._changelog_coverage["state"] == "ok"
    assert asyncio.run(restarted._collect_changelog([item()], IDENTITY)) == []
    assert offsets == [0, 2, 2]
    assert all(request.method == "GET" and request.url.host == "example.atlassian.net" for request in requests)
    serialized = json.dumps(restarted.checkpoint())
    assert "synthetic-token-never-cache" not in serialized
    assert "Open" not in serialized and "Engineer" not in serialized


def test_authoritative_events_have_stable_ids_aliases_and_safe_bounded_field_summaries():
    fields = [
        {"field": "description", "fromString": "private old body", "toString": "private new body"},
        {"field": "Custom body", "fieldId": "customfield_10000", "toString": "private custom body"},
        {"field": "assignee", "fromString": "Old <old@example.test>", "toString": "New <new@example.test>"},
        {"field": "status", "fromString": "Open", "toString": "Done"},
    ]
    event = changelog_activity({"key": "ENG-1", "id": item().id, "title": item().title,
                               "url": item().url, "reasons": ["assigned"]}, change(fields=fields))
    assert event.id == "jira:issue:ENG-1:change:10001"
    assert event.legacy_activity_id == f"jira:issue:ENG-1:updated:{NOW.isoformat()}"
    assert event.detail_source == "Jira change history" and event.detail_status == "captured"
    assert event.actor == "Engineer"
    assert event.changes == ["Description changed", "Custom body changed", "Assignee: Old → New", "Status: Open → Done"]
    serialized = event.model_dump_json()
    assert "private" not in serialized and "@" not in serialized
    assert len(jira_changes(fields * 25)) == 40
    administrative = changelog_activity({"key": "ENG-1", "id": item().id, "title": item().title,
                                         "url": item().url, "reasons": []}, change(fields=[]))
    assert administrative.detail_status == "administrative"


def test_expired_rows_advance_offsets_and_retention_expansion_recovers_them(monkeypatch):
    old = change("old", when=NOW - timedelta(days=80))
    offsets = []

    def handler(request):
        offset = int(request.url.params["startAt"])
        offsets.append(offset)
        return httpx.Response(200, json=page([old], offset))

    transport(monkeypatch, handler)
    connector = target()
    assert asyncio.run(connector._collect_changelog([item()], IDENTITY)) == []
    assert connector._changelog.records["ENG-1"]["offset"] == 1
    connector.retention_days = 120
    assert [event.source_update_id for event in asyncio.run(connector._collect_changelog([item()], IDENTITY))] == ["old"]
    assert offsets == [0, 0]


def test_failed_item_does_not_starve_later_items_across_saved_rotation(monkeypatch):
    keys = []

    def handler(request):
        key = request.url.path.split("/")[-2]
        keys.append(key)
        return httpx.Response(503, text="private server response") if key == "ENG-1" else httpx.Response(200, json=page([change()]))

    transport(monkeypatch, handler)
    connector = target(changelog_batch_size=1)
    assert asyncio.run(connector._collect_changelog([item(), item("ENG-2")], IDENTITY)) == []
    assert connector._changelog.records["ENG-1"]["offset"] == 0
    restarted = target(changelog_batch_size=1)
    restarted.load_state(connector.checkpoint())
    assert len(asyncio.run(restarted._collect_changelog([item(), item("ENG-2")], IDENTITY))) == 1
    assert keys == ["ENG-1", "ENG-2"]
    assert restarted._changelog_coverage["failed"] == 1
    assert restarted._changelog_coverage["failure_diagnostics"] == [
        {"item_key": "ENG-1", "error_code": "JIRA_CHANGELOG_HTTP_503", "retry_at": ""}]
    assert "private server response" not in json.dumps(restarted.checkpoint())


@pytest.mark.parametrize("bad", [
    {"id": "10002", "created": "missing", "items": []},
    {"id": "10001", "created": NOW.isoformat(), "items": []},
    {"id": "bad/id", "created": NOW.isoformat(), "items": []},
    {"id": "10002", "created": NOW.isoformat(), "items": [{"field": None}]},
])
def test_malformed_page_accepts_neither_its_events_nor_offset(monkeypatch, bad):
    transport(monkeypatch, lambda request: httpx.Response(200, json=page([change(), bad])))
    connector = target()
    assert asyncio.run(connector._collect_changelog([item()], IDENTITY)) == []
    assert connector._changelog.records["ENG-1"]["offset"] == 0
    assert connector._changelog.records["ENG-1"]["seen"] == {}
    assert connector._changelog.records["ENG-1"]["error_code"] == "JIRA_CHANGELOG_INVALID_PAGE"


def test_rate_limit_retry_after_survives_restart_without_hammering_ticket(monkeypatch):
    requests = transport(monkeypatch, lambda request: httpx.Response(429, headers={"Retry-After": "3600"}, text="private"))
    connector = target()
    assert asyncio.run(connector._collect_changelog([item()], IDENTITY)) == []
    restarted = target()
    restarted.load_state(connector.checkpoint())
    assert asyncio.run(restarted._collect_changelog([item()], IDENTITY)) == []
    assert len([request for request in requests if request.url.path.endswith("/changelog")]) == 1
    assert restarted._changelog_coverage["failed"] == 1


def test_missing_token_has_explicit_unavailable_coverage_and_no_network(monkeypatch):
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.setattr(jira_module.httpx, "AsyncClient", lambda **kwargs: pytest.fail("Network must not start"))
    connector = target()
    assert asyncio.run(connector._collect_changelog([item()], IDENTITY)) == []
    assert connector._changelog_coverage["state"] == "unavailable"
    assert connector._changelog_coverage["reason"] == "JIRA_CHANGELOG_TOKEN_UNAVAILABLE"
    event = Activity(id="generic", source="jira", event_type="updated", item_id=item().id,
                     item_key=item().key, item_title=item().title, timestamp=NOW, summary="Issue updated", url=item().url)
    connector._mark_changelog_details([event])
    assert event.detail_status == "unavailable"


def test_disabled_changelog_has_no_auth_or_progress_side_effects(monkeypatch):
    monkeypatch.setattr(jira_module.httpx, "AsyncClient", lambda **kwargs: pytest.fail("Network must not start"))
    connector = JiraConnector(JiraSettings(), 30)
    assert asyncio.run(connector._collect_changelog([item()], IDENTITY)) == []
    assert connector._changelog_coverage["state"] == "disabled"
    assert not connector._changelog_coverage["incomplete"]
    assert "changelog" not in connector.checkpoint()


@pytest.mark.parametrize("site", ["http://example.atlassian.net", "https://token@example.atlassian.net",
                                  "https://example.atlassian.net/other", "https://elsewhere.test",
                                  "https://example.atlassian.net?redirect=elsewhere", "https://example.atlassian.net:8443"])
def test_invalid_origin_never_receives_credentials(monkeypatch, site):
    monkeypatch.setenv("JIRA_API_TOKEN", "synthetic-token-never-cache")
    monkeypatch.setattr(jira_module.httpx, "AsyncClient", lambda **kwargs: pytest.fail("Network must not start"))
    connector = target()
    connector.settings = JiraSettings(site=site, expected_account="me@example.test", changelog_enabled=True)
    assert asyncio.run(connector._collect_changelog([item()], IDENTITY)) == []
    assert connector._changelog_coverage["reason"] == "JIRA_CHANGELOG_INVALID_ORIGIN"


def test_rest_account_must_match_verified_cli_account_before_any_history_read(monkeypatch):
    requests = transport(monkeypatch, lambda request: pytest.fail("No changelog should be read"))
    connector = target()
    identity = {**IDENTITY, "account_id": "different-cli-account"}
    assert asyncio.run(connector._collect_changelog([item()], identity)) == []
    assert len(requests) == 1
    assert connector._changelog_coverage["reason"] == "JIRA_CHANGELOG_IDENTITY_MISMATCH"


def test_redirect_is_not_followed_or_reported_with_raw_private_response(monkeypatch):
    requests = transport(monkeypatch, lambda request: httpx.Response(302, headers={"Location": "https://evil.example/steal"}, text="private"))
    connector = target()
    assert asyncio.run(connector._collect_changelog([item()], IDENTITY)) == []
    assert len(requests) == 2
    assert connector._changelog.records["ENG-1"]["error_code"] == "JIRA_CHANGELOG_HTTP_302"


def test_expired_phase_budget_is_pending_without_false_failed_ticket(monkeypatch):
    requests = transport(monkeypatch, lambda request: pytest.fail("No changelog should be read"))
    connector = target()
    connector._deadline = time.monotonic() - 1
    assert asyncio.run(connector._collect_changelog([item()], IDENTITY)) == []
    assert requests == []
    assert connector._changelog_coverage["budget_exhausted"]
    assert connector._changelog_coverage["failed"] == 0
    assert connector._changelog.records["ENG-1"]["status"] == "pending"


@pytest.mark.parametrize("request_limit,phase_limit,budget_exhausted", [(0.01, 1, False), (1, 0.01, True)])
def test_stalled_identity_has_whole_request_timeout_and_is_canceled(monkeypatch, request_limit, phase_limit, budget_exhausted):
    monkeypatch.setenv("JIRA_API_TOKEN", "synthetic-token-never-cache")
    real_client = httpx.AsyncClient
    canceled = []

    async def handler(request):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            canceled.append(request.url.path)
            raise

    monkeypatch.setattr(jira_module.httpx, "AsyncClient", lambda **kwargs:
                        real_client(transport=httpx.MockTransport(handler), **kwargs))
    connector = target(changelog_timeout_seconds=phase_limit, changelog_request_timeout_seconds=request_limit)
    assert asyncio.run(connector._collect_changelog([item()], IDENTITY)) == []
    assert canceled == ["/rest/api/3/myself"]
    assert connector._changelog_coverage["budget_exhausted"] is budget_exhausted
    assert connector._changelog_coverage["transport_failed"] is not budget_exhausted
    assert connector._changelog_coverage["reason"] == ("" if budget_exhausted else "JIRA_CHANGELOG_TIMEOUT")
    assert connector._changelog.records["ENG-1"]["offset"] == 0


def test_rate_limit_stops_other_ticket_reads_and_identity_retry_until_due(monkeypatch):
    requests = transport(monkeypatch, lambda request: httpx.Response(429, headers={"Retry-After": "3600"}))
    connector = target()
    assert asyncio.run(connector._collect_changelog([item(), item("ENG-2")], IDENTITY)) == []
    assert len(requests) == 2
    assert connector._changelog_coverage["reason"] == "JIRA_CHANGELOG_HTTP_429"
    assert connector._changelog_coverage["retry_at"] == connector._changelog.retry_at
    restarted = target()
    restarted.load_state(connector.checkpoint())
    assert asyncio.run(restarted._collect_changelog([item(), item("ENG-2")], IDENTITY)) == []
    assert len(requests) == 2


def test_unmatched_generic_is_unavailable_after_successful_empty_source_history(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json=page([])))
    connector = target()
    assert asyncio.run(connector._collect_changelog([item()], IDENTITY)) == []
    event = Activity(id="generic", source="jira", event_type="updated", item_id=item().id,
                     item_key=item().key, item_title=item().title, timestamp=NOW, summary="Issue updated", url=item().url)
    connector._mark_changelog_details([event])
    assert connector._changelog_coverage["state"] == "ok"
    assert event.detail_status == "unavailable"


def test_retained_hidden_or_inactive_parent_is_backfilled_without_reactivation(monkeypatch):
    requests = transport(monkeypatch, lambda request: httpx.Response(200, json=page([change()])))
    connector = target()
    retained = item().model_dump(mode="json")
    retained["metadata"] = {"snapshot_only": True, "latest_activity_at": NOW.isoformat()}
    connector.history_inventory_loader = lambda scope: [retained]
    events = asyncio.run(connector._collect_changelog([], IDENTITY))
    assert [event.item_key for event in events] == ["ENG-1"]
    assert len(requests) == 2
    assert retained["metadata"]["snapshot_only"]


def test_scope_change_discards_prior_account_progress_and_seen_ids():
    history = JiraChangelogHistory("first")
    history.prepare({"ENG-1"}, NOW - timedelta(days=60), 60)
    history.accept("ENG-1", 0, 1, True, {"10001": NOW.isoformat()})
    assert JiraChangelogHistory("other", history.checkpoint()).records == {}


def test_inventory_failure_preserves_cursors_for_retained_items(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json=page([change()])))
    connector = target()
    asyncio.run(connector._collect_changelog([item()], IDENTITY))

    def unavailable(scope):
        raise OSError("Synthetic cache inventory failure")

    connector.history_inventory_loader = unavailable
    asyncio.run(connector._collect_changelog([], IDENTITY))
    assert "ENG-1" in connector._changelog.records
    assert connector._changelog_coverage["inventory_failed"]


def test_cancelled_read_has_no_accepted_page_progress(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "synthetic-token-never-cache")
    started = None
    real_client = httpx.AsyncClient

    async def scenario():
        nonlocal started
        started = asyncio.Event()

        async def handler(request):
            if request.url.path.endswith("/myself"):
                return httpx.Response(200, json={"accountId": "self"})
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(jira_module.httpx, "AsyncClient", lambda **kwargs:
                            real_client(transport=httpx.MockTransport(handler), **kwargs))
        connector = target()
        task = asyncio.create_task(connector._collect_changelog([item()], IDENTITY))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert connector._changelog.records["ENG-1"]["offset"] == 0
        assert connector._changelog.records["ENG-1"]["seen"] == {}

    asyncio.run(scenario())


def test_refresh_returns_combined_checkpoint_and_captured_events(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json=page([change()])))
    connector = target()

    async def authenticate(executable):
        return dict(IDENTITY)

    async def search(executable, jql, *, limit=None):
        return [{"key": "ENG-1", "fields": {"status": {"name": "Open"}}}] if jql.startswith("assignee =") else []

    async def view(executable, key):
        return {"key": key, "fields": {"summary": "Synthetic ENG-1", "status": {"name": "Open"},
                "updated": NOW.isoformat(), "assignee": {"accountId": "self"}}}

    monkeypatch.setattr(connector, "_find_cli", lambda: "synthetic-acli")
    monkeypatch.setattr(connector, "_authenticate", authenticate)
    monkeypatch.setattr(connector, "_search", search)
    monkeypatch.setattr(connector, "_view", view)
    result = asyncio.run(connector.refresh())
    assert result.health.state in {"ok", "partial"}
    assert set(result.health.coverage["unavailable_queries"]) <= {"closed_history:rotating_batch"}
    assert result.checkpoint["version"] == 1 and result.checkpoint["changelog"]["scope"] == SCOPE
    assert result.checkpoint["changelog"]["records"]["ENG-1"]["offset"] == 1
    assert [event.source_update_id for event in result.activities if event.source_update_id] == ["10001"]


def test_nonadvancing_or_unbounded_pages_are_rejected():
    with pytest.raises(ValueError):
        parse_changelog_page(page([], last=False, total=4), 0, 100)
    with pytest.raises(ValueError):
        parse_changelog_page(page([change()], offset=3), 0, 100)
    with pytest.raises(ValueError):
        parse_changelog_page(page([change(), change("10002")]), 0, 1)
