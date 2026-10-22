import asyncio
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx

from config import AzureDevOpsSettings, source_scope
from connectors.ado_history import AdoHistory
from connectors.azure_devops import AzureDevOpsConnector
from models import ConnectorHealth, ConnectorResult, utc_now
from store import Store


NOW = utc_now() - timedelta(minutes=1)


def item(key="1", when=NOW):
    return {"id": f"ado:workitem:{key}", "key": key, "source": "ado", "title": "Synthetic",
            "updated_at": when.isoformat(), "url": f"https://example.test/{key}", "reasons": ["assigned"]}


def update(identifier, *, revision=16, when=NOW, fields=None):
    return {"id": identifier, "rev": revision, "revisedDate": "9999-01-01T00:00:00Z",
            "revisedBy": {"displayName": "Synthetic Actor"},
            "fields": {"System.ChangedDate": {"newValue": when.isoformat()},
                       **(fields if fields is not None else {"System.State": {"oldValue": "New", "newValue": "Active"}})}}


def connector(monkeypatch, **settings):
    target = AzureDevOpsConnector(AzureDevOpsSettings(organization="synthetic", **settings), 30)
    target.verified_cache_scope = source_scope("azure_devops", "synthetic", "me@example.test")

    async def metadata(client, deadline):
        return "captured"

    monkeypatch.setattr(target, "_history_metadata", metadata)
    return target


def server(monkeypatch, target, rows):
    calls = []

    async def read(client, method, url, **kwargs):
        key = urlsplit(url).path.split("/")[-2]
        query = parse_qs(urlsplit(url).query)
        offset, size = int(query["$skip"][0]), int(query["$top"][0])
        calls.append((key, offset, size))
        values = rows.get(key, []) if isinstance(rows, dict) else rows
        return {"value": values[offset:offset + size]}, {}

    monkeypatch.setattr(target, "_json", read)
    return calls


def test_update_count_and_revision_are_independent_and_all_events_survive(monkeypatch):
    target = connector(monkeypatch)
    calls = server(monkeypatch, target, [update(n, revision=min(n, 16)) for n in range(1, 23)])
    events = asyncio.run(target._capture_history(None, [item()]))
    assert calls == [("1", 0, 100)]
    assert [event.source_update_id for event in events] == list(range(1, 23))
    assert events[-1].source_revision == 16
    assert events[-1].legacy_activity_id == f"ado:workitem:1:updated:{NOW.isoformat()}"
    assert target.checkpoint()["records"]["1"]["offset"] == 22


def test_pagination_restart_overlap_dedup_and_intervening_updates(monkeypatch):
    target = connector(monkeypatch, history_page_size=2)
    rows = [update(n) for n in range(1, 6)]
    calls = server(monkeypatch, target, rows)
    first = asyncio.run(target._capture_history(None, [item()]))
    assert [event.source_update_id for event in first] == [1, 2]
    assert target._history_coverage["pending"] == 1
    reopened = connector(monkeypatch, history_page_size=2)
    reopened.load_state(target.checkpoint())
    followup_calls = server(monkeypatch, reopened, rows)
    second = asyncio.run(reopened._capture_history(None, [item()]))
    third = asyncio.run(reopened._capture_history(None, [item()]))
    assert [event.source_update_id for event in second + third] == [3, 4, 5]
    assert followup_calls == [("1", 2, 2), ("1", 4, 2)]
    rows.extend([update(6), update(7)])
    fourth = asyncio.run(reopened._capture_history(None, [item()]))
    fifth = asyncio.run(reopened._capture_history(None, [item()]))
    assert [event.source_update_id for event in fourth + fifth] == [6, 7]
    assert followup_calls[-2:] == [("1", 4, 2), ("1", 6, 2)]
    assert calls == [("1", 0, 2)]


def test_more_than_fifty_items_rotate_without_starvation_and_retry_failures(monkeypatch):
    target = connector(monkeypatch, history_batch_size=50)
    inventory = [item(str(n)) for n in range(1, 102)]
    calls = server(monkeypatch, target, {str(n): [update(1)] for n in range(1, 102)})
    for _ in range(3):
        asyncio.run(target._capture_history(None, inventory))
    assert {key for key, _, _ in calls} == {str(n) for n in range(1, 102)}
    assert target._history_coverage["pending"] == 0
    assert target._history_coverage["captured"] == 101


def test_failed_page_does_not_advance_and_retry_uses_same_position(monkeypatch):
    target = connector(monkeypatch, history_page_size=2)
    rows = [update(n) for n in range(1, 4)]
    server(monkeypatch, target, rows)
    asyncio.run(target._capture_history(None, [item()]))

    async def denied(client, method, url, **kwargs):
        request = httpx.Request("GET", url)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("Access denied", request=request, response=response)

    monkeypatch.setattr(target, "_json", denied)
    assert asyncio.run(target._capture_history(None, [item()])) == []
    assert target.checkpoint()["records"]["1"]["offset"] == 2
    assert target._history_coverage["failed"] == 1
    target._history.records["1"]["retry_at"] = ""
    calls = server(monkeypatch, target, rows)
    retried = asyncio.run(target._capture_history(None, [item()]))
    assert calls == [("1", 2, 2)]
    assert [event.source_update_id for event in retried] == [3]
    assert target._history_coverage["failed"] == 0


def test_transient_rate_limit_is_retried_within_history_budget(monkeypatch):
    target = connector(monkeypatch)
    calls = []

    async def limited(client, method, url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            request = httpx.Request("GET", url)
            response = httpx.Response(429, headers={"Retry-After": "0"}, request=request)
            raise httpx.HTTPStatusError("Rate limited", request=request, response=response)
        return {"value": [update(1)]}, {}

    monkeypatch.setattr(target, "_json", limited)
    assert len(asyncio.run(target._capture_history(None, [item()]))) == 1
    assert len(calls) == 2


def test_phase_deadline_defers_without_failure_and_rotation_moves_past_slow_item(monkeypatch):
    target = connector(monkeypatch, history_timeout_seconds=.02, history_batch_size=1)

    async def slow(client, method, url, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(target, "_json", slow)
    assert asyncio.run(target._capture_history(None, [item("1"), item("2")])) == []
    assert target._history_coverage["failed"] == 0
    assert target._history_coverage["budget_deferred"] == 1
    assert target._history_coverage["pending"] == 2
    calls = server(monkeypatch, target, {"2": [update(1)]})
    result = asyncio.run(target._capture_history(None, [item("1"), item("2")]))
    assert result[0].item_key == "2"
    assert calls[0][0] == "2"


def test_scope_mismatch_cannot_reuse_progress_or_metadata():
    original = AdoHistory("first")
    original.prepare({"1"}, NOW - timedelta(days=60))
    original.accept("1", 0, 12, 100, {"12": NOW.isoformat()})
    original.field_metadata = {"Custom.Foo": {"name": "Foo", "type": "string"}}
    switched = AdoHistory("other", original.checkpoint())
    assert switched.records == {}
    assert switched.field_metadata == {}


def test_retention_widening_after_restart_revisits_expired_rows_without_store_duplicates(monkeypatch, tmp_path):
    rows = [update(1, when=NOW - timedelta(days=70)), update(2, when=NOW - timedelta(days=1))]
    target = connector(monkeypatch)
    server(monkeypatch, target, rows)
    recent = asyncio.run(target._capture_history(None, [item()]))
    assert [event.source_update_id for event in recent] == [2]
    assert target.checkpoint()["retention_days"] == 60
    store = Store(tmp_path / "retention.db")
    store.initialize()

    def accept(events, checkpoint, days):
        store.replace_connector(ConnectorResult(
            connector="azure_devops", activities=events, checkpoint=checkpoint,
            health=ConnectorHealth(connector="azure_devops", state="ok", message="Synthetic"),
        ), days)

    accept(recent, target.checkpoint(), 60)
    reopened = connector(monkeypatch)
    reopened.retention_days = 90
    reopened.load_state(target.checkpoint())
    calls = server(monkeypatch, reopened, rows)
    widened = asyncio.run(reopened._capture_history(None, [item()]))
    assert calls == [("1", 0, 100)]
    assert [event.source_update_id for event in widened] == [1, 2]
    accept(widened, reopened.checkpoint(), 90)
    with store._connect() as connection:
        stored = connection.execute("SELECT entity_id FROM entities WHERE entity_kind='activity' ORDER BY entity_id").fetchall()
    assert [row["entity_id"] for row in stored] == ["ado:workitem:1:update:1", "ado:workitem:1:update:2"]
    assert reopened.checkpoint()["retention_days"] == 90


def test_unknown_checkpoint_retention_rescans_once_and_shrink_then_widen_rescans():
    history = AdoHistory("verified")
    history.prepare({"1"}, NOW - timedelta(days=60))
    history.accept("1", 0, 22, 100, {"22": NOW.isoformat()})
    legacy = history.checkpoint()
    legacy.pop("retention_days")
    reopened = AdoHistory("verified", legacy)
    reopened.set_retention_days(60)
    assert reopened.records["1"]["offset"] == 0
    assert reopened.records["1"]["seen"] == {}
    reopened.accept("1", 0, 22, 100, {"22": NOW.isoformat()})
    same = AdoHistory("verified", reopened.checkpoint())
    same.set_retention_days(60)
    assert same.records["1"]["offset"] == 22
    same.set_retention_days(30)
    assert same.records["1"]["offset"] == 22
    same.set_retention_days(60)
    assert same.records["1"]["offset"] == 0


def test_retained_inactive_inventory_is_scanned_independently_of_mention_window(monkeypatch):
    target = connector(monkeypatch, mention_reply_days=0)
    older = NOW - timedelta(days=40)
    target.history_inventory_loader = lambda scope: [item("8", older)]
    calls = server(monkeypatch, target, {"8": [update(4, when=older)]})
    events = asyncio.run(target._capture_history(None, []))
    assert calls == [("8", 0, 100)]
    assert events[0].source_update_id == 4


def test_current_old_item_still_captures_recent_relation_update_without_changed_date(monkeypatch):
    target = connector(monkeypatch, mention_reply_days=0)
    old_item = item("8", NOW - timedelta(days=90))
    rows = [update(1, when=NOW - timedelta(days=90)),
            {"id": 2, "rev": 16, "revisedDate": NOW.isoformat(), "fields": {},
             "relations": {"added": [{"rel": "System.LinkTypes.Related", "url": "https://example.test/related"}]},
             "revisedBy": {"displayName": "Synthetic Actor"}}]
    calls = server(monkeypatch, target, {"8": rows})
    events = asyncio.run(target._capture_history(None, [old_item]))
    assert calls == [("8", 0, 100)]
    assert [event.source_update_id for event in events] == [2]
    assert events[0].timestamp == NOW
    assert events[0].legacy_activity_id is None


def test_inactive_cached_parent_without_retained_activity_is_not_scanned(monkeypatch):
    target = connector(monkeypatch)
    old_parent = item("8", NOW - timedelta(days=90))
    old_parent["metadata"] = {"snapshot_only": True}
    target.history_inventory_loader = lambda scope: [old_parent]
    calls = server(monkeypatch, target, {"8": [update(1)]})
    assert asyncio.run(target._capture_history(None, [])) == []
    assert calls == []


def test_metadata_cache_is_sanitized_identity_aware_and_outage_visible(monkeypatch):
    target = AzureDevOpsConnector(AzureDevOpsSettings(organization="synthetic"), 30)
    target.verified_cache_scope = "verified"
    calls = []

    async def read(client, method, url, **kwargs):
        calls.append(url)
        if "/fields?" in url:
            return {"value": [{"referenceName": "Custom.Owner", "name": "Owner", "type": "string", "isIdentity": True,
                               "description": "must not be saved", "unused": {"secret": "must not be saved"}}]}, {}
        return {"value": [update(1)]}, {}

    monkeypatch.setattr(target, "_json", read)
    asyncio.run(target._capture_history(None, [item()]))
    assert target.checkpoint()["field_metadata"] == {"Custom.Owner": {"name": "Owner", "type": "identity"}}
    asyncio.run(target._capture_history(None, [item()]))
    assert sum("/fields?" in url for url in calls) == 1
    target._history.metadata_at = ""

    async def unavailable(client, method, url, **kwargs):
        if "/fields?" in url:
            raise ValueError("Unavailable field catalogue")
        return {"value": [update(2)]}, {}

    monkeypatch.setattr(target, "_json", unavailable)
    asyncio.run(target._capture_history(None, [item()]))
    assert target._history_coverage["metadata_status"] == "failed"
    assert target._history_coverage["incomplete"]
    assert target.checkpoint()["field_metadata"]["Custom.Owner"]["type"] == "identity"


def test_update_ids_dedup_same_page_and_admin_actor_is_retained(monkeypatch):
    target = connector(monkeypatch)
    administrative = update(1, fields={})
    server(monkeypatch, target, [administrative, administrative, update(2)])
    result = asyncio.run(target._capture_history(None, [item()]))
    assert len(result) == 2
    assert result[0].detail_status == "administrative"
    assert result[0].actor == "Synthetic Actor"
    assert result[0].detail_source == "ADO update history"
    assert target.checkpoint()["records"]["1"]["offset"] == 3


def test_invalid_page_and_cancellation_do_not_accept_partial_events(monkeypatch):
    target = connector(monkeypatch)
    server(monkeypatch, target, [update(1), {"rev": 16}])
    assert asyncio.run(target._capture_history(None, [item()])) == []
    assert target.checkpoint()["records"]["1"]["offset"] == 0
    assert target.checkpoint()["records"]["1"]["seen"] == {}


def test_undated_sentinel_update_does_not_fabricate_event_timestamp(monkeypatch):
    target = connector(monkeypatch)
    server(monkeypatch, target, [{"id": 1, "rev": 16, "revisedDate": "9999-01-01T00:00:00Z", "fields": {}}])
    assert asyncio.run(target._capture_history(None, [item()])) == []
    assert target._history_coverage["unavailable_timestamps"] == 1
    assert target._history_coverage["unavailable"] == 1
    assert target.checkpoint()["records"]["1"]["offset"] == 1


def test_undated_field_update_cannot_use_next_revision_boundary_as_event_time(monkeypatch):
    target = connector(monkeypatch)
    row = {"id": 1, "rev": 2, "revisedDate": NOW.isoformat(),
           "fields": {"System.State": {"oldValue": "New", "newValue": "Active"}}}
    server(monkeypatch, target, [row])
    assert asyncio.run(target._capture_history(None, [item()])) == []
    assert target._history_coverage["unavailable_timestamps"] == 1


def test_undated_overlap_can_recover_real_timestamp_including_expired_update(monkeypatch):
    for when in (NOW, NOW - timedelta(days=90)):
        target = connector(monkeypatch)
        rows = [{"id": 1, "rev": 2, "revisedDate": "9999-01-01T00:00:00Z", "fields": {}}]
        server(monkeypatch, target, rows)
        assert asyncio.run(target._capture_history(None, [item()])) == []
        rows[0] = update(1, when=when)
        result = asyncio.run(target._capture_history(None, [item()]))
        assert len(result) == (1 if when == NOW else 0)
        assert target._history_coverage["unavailable_timestamps"] == 0
        assert target._history_coverage["captured"] == 1


def test_undated_gap_before_last_page_is_retried_without_duplicate_events(monkeypatch):
    target = connector(monkeypatch, history_page_size=2)
    rows = [{"id": 1, "rev": 2, "revisedDate": "9999-01-01T00:00:00Z", "fields": {}}, update(2), update(3)]
    calls = server(monkeypatch, target, rows)
    first = asyncio.run(target._capture_history(None, [item()]))
    second = asyncio.run(target._capture_history(None, [item()]))
    assert [event.source_update_id for event in first + second] == [2, 3]
    assert target._history_coverage["unavailable_timestamps"] == 1
    rows[0] = update(1)
    third = asyncio.run(target._capture_history(None, [item()]))
    fourth = asyncio.run(target._capture_history(None, [item()]))
    assert [event.source_update_id for event in third + fourth] == [1]
    assert calls == [("1", 0, 2), ("1", 2, 2), ("1", 0, 2), ("1", 2, 2)]
    assert target._history_coverage["unavailable_timestamps"] == 0


def test_generic_placeholder_summary_is_legacy_exact_for_pending_and_failure(monkeypatch):
    target = connector(monkeypatch, history_batch_size=1)

    async def query(client, where):
        return [1, 2]

    async def projects(client):
        return []

    async def hydrate(client, keys):
        return [{"id": key, "rev": 2, "fields": {
            "System.WorkItemType": "User Story", "System.Title": "Synthetic",
            "System.ChangedDate": NOW.isoformat()}} for key in keys]

    async def categories(client, records):
        return {}

    async def denied(client, method, url, **kwargs):
        request = httpx.Request("GET", url)
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("Access denied", request=request, response=response)

    monkeypatch.setattr(target, "_wiql", query)
    monkeypatch.setattr(target, "_projects", projects)
    monkeypatch.setattr(target, "_hydrate", hydrate)
    monkeypatch.setattr(target, "_state_categories", categories)
    monkeypatch.setattr(target, "_json", denied)
    _, events, _ = asyncio.run(target._work_items(None, {}))
    assert [event.summary for event in events] == ["User Story updated", "User Story updated"]
    assert [event.detail_status for event in events] == ["unavailable", "pending"]


def test_cancellation_replays_from_last_committed_checkpoint(monkeypatch):
    target = connector(monkeypatch, history_batch_size=2)

    async def read(client, method, url, **kwargs):
        if "/1/updates?" in url:
            return {"value": [update(1)]}, {}
        raise asyncio.CancelledError()

    monkeypatch.setattr(target, "_json", read)
    try:
        asyncio.run(target._capture_history(None, [item("1"), item("2")]))
    except asyncio.CancelledError:
        pass
    # In-memory work exists but no ConnectorResult was accepted; a coordinator
    # restart/retry restores its committed checkpoint, not that partial state.
    target.load_state({})
    calls = server(monkeypatch, target, {"1": [update(1)], "2": [update(1)]})
    events = asyncio.run(target._capture_history(None, [item("1"), item("2")]))
    assert len(events) == 2
    assert calls == [("1", 0, 100), ("2", 0, 100)]
