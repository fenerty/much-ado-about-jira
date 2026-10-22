import asyncio
import json
import time
from datetime import timedelta

import httpx
import pytest

from config import AzureDevOpsSettings
from connectors.ado_history import AdoHistory
from connectors.azure_devops import AzureDevOpsConnector, HistoryBudgetDeferred
from models import utc_now


NOW = utc_now() - timedelta(minutes=1)


def item(key="1"):
    return {"id": f"ado:workitem:{key}", "key": key, "source": "ado", "title": "Synthetic",
            "updated_at": NOW.isoformat(), "url": "https://example.test/item", "reasons": ["assigned"]}


def update():
    return {"id": 1, "rev": 1, "fields": {
        "System.ChangedDate": {"newValue": NOW.isoformat()},
        "System.State": {"oldValue": "New", "newValue": "Active"}}}


def connector(monkeypatch, **settings):
    target = AzureDevOpsConnector(AzureDevOpsSettings(organization="synthetic", **settings), 30)
    target.verified_cache_scope = "verified"

    async def metadata(client, deadline):
        return "cached"

    monkeypatch.setattr(target, "_history_metadata", metadata)
    return target


def status_error(status, *, headers=None):
    request = httpx.Request("GET", "https://example.test/private?token=must-not-persist")
    response = httpx.Response(status, request=request, headers=headers,
                              content=b"private title, actor, description must not persist")
    return httpx.HTTPStatusError("private body and URL must not persist", request=request, response=response)


def test_deleted_requires_matching_recycle_bin_confirmation_and_survives_restart(monkeypatch):
    target = connector(monkeypatch)
    target.history_inventory_loader = lambda scope: [item()]
    calls = []

    async def read(client, method, url):
        calls.append(url)
        if "/recyclebin/" in url:
            return {"id": "1", "deletedBy": "must not persist", "resource": {"id": 1}}, {}
        raise status_error(404)

    monkeypatch.setattr(target, "_json", read)
    assert asyncio.run(target._capture_history(None, [])) == []
    progress = target._history_coverage
    assert progress["deleted"] == 1 and progress["failed"] == progress["remaining"] == 0
    assert progress["cooling_down"] == 1
    assert progress["failure_diagnostics"][0]["error_code"] == "ADO_RECYCLE_BIN_CONFIRMED"
    assert len(calls) == 2
    saved = target.checkpoint()
    assert "must not persist" not in json.dumps(saved)
    reopened = connector(monkeypatch)
    reopened.load_state(saved)
    reopened.history_inventory_loader = lambda scope: [item()]
    monkeypatch.setattr(reopened, "_json", read)
    assert asyncio.run(reopened._capture_history(None, [])) == []
    assert len(calls) == 2
    reopened._history.records["1"]["retry_at"] = (utc_now() - timedelta(seconds=1)).isoformat()

    async def restored(client, method, url):
        calls.append(url)
        return {"value": [update()]}, {}

    monkeypatch.setattr(reopened, "_json", restored)
    events = asyncio.run(reopened._capture_history(None, []))
    assert len(events) == 1
    assert reopened._history_coverage["captured"] == 1
    assert reopened._history_coverage["deleted"] == 0
    assert reopened.checkpoint()["records"]["1"]["error_code"] == ""


def test_visible_restored_item_clears_deleted_cooldown_without_timestamp_change(monkeypatch):
    target = connector(monkeypatch)
    history = target._ensure_history()
    history.set_retention_days(60)
    history.prepare({"1"}, NOW - timedelta(days=60))
    history.observe("1", NOW)
    history.fail("1", "ADO_RECYCLE_BIN_CONFIRMED", category="deleted", retry_seconds=1800)

    async def restored(client, method, url):
        return {"value": [update()]}, {}

    monkeypatch.setattr(target, "_json", restored)
    assert len(asyncio.run(target._capture_history(None, [item()]))) == 1
    assert target._history_coverage["deleted"] == 0


@pytest.mark.parametrize("recycle_response", [None, {"id": "2"}, {"id": True}])
def test_404_without_matching_confirmation_is_inaccessible_not_deleted(monkeypatch, recycle_response):
    target = connector(monkeypatch)

    async def read(client, method, url):
        if "/recyclebin/" in url and recycle_response is not None:
            return recycle_response, {}
        raise status_error(404)

    monkeypatch.setattr(target, "_json", read)
    asyncio.run(target._capture_history(None, [item()]))
    assert target._history_coverage["deleted"] == target._history_coverage["failed"] == 0
    assert target._history_coverage["inaccessible"] == 1
    assert target._history_coverage["failure_diagnostics"][0]["error_code"] == "HTTP_404_UNCONFIRMED"


def test_access_denial_cools_down_and_restores_after_retry(monkeypatch):
    target = connector(monkeypatch)
    calls = []

    async def denied(client, method, url):
        calls.append(url)
        raise status_error(403)

    monkeypatch.setattr(target, "_json", denied)
    asyncio.run(target._capture_history(None, [item()]))
    asyncio.run(target._capture_history(None, [item()]))
    assert len(calls) == 1
    assert target._history_coverage["inaccessible"] == 1
    target._history.records["1"]["retry_at"] = ""

    async def restored(client, method, url):
        return {"value": [update()]}, {}

    monkeypatch.setattr(target, "_json", restored)
    assert len(asyncio.run(target._capture_history(None, [item()]))) == 1
    assert target._history_coverage["failure_diagnostics"] == []


def test_request_timeout_uses_configuration_and_transient_failures_back_off(monkeypatch):
    target = connector(monkeypatch, history_request_timeout_seconds=.01, history_timeout_seconds=.2)
    calls = []

    async def slow(client, method, url):
        calls.append(url)
        await asyncio.sleep(1)

    monkeypatch.setattr(target, "_json", slow)
    asyncio.run(target._capture_history(None, [item()]))
    assert len(calls) == 2
    diagnostic = target._history_coverage["failure_diagnostics"][0]
    assert diagnostic["failure_category"] == "transient"
    assert diagnostic["error_code"] == "TimeoutError"
    assert diagnostic["consecutive_failures"] == 1 and diagnostic["retry_at"]
    asyncio.run(target._capture_history(None, [item()]))
    assert len(calls) == 2
    target._history.records["1"]["retry_at"] = ""
    asyncio.run(target._capture_history(None, [item()]))
    assert target._history_coverage["failure_diagnostics"][0]["consecutive_failures"] == 2


def test_shared_phase_timeout_is_deferred_without_recording_a_failed_read(monkeypatch):
    target = connector(monkeypatch, history_request_timeout_seconds=1, history_timeout_seconds=.01)

    async def slow(client, method, url):
        await asyncio.sleep(1)

    monkeypatch.setattr(target, "_json", slow)
    assert asyncio.run(target._capture_history(None, [item()])) == []
    progress = target._history_coverage
    assert progress["budget_deferred"] == 1
    assert progress["failed"] == 0 and progress["pending"] == 1
    assert progress["failure_diagnostics"] == []
    assert target.checkpoint()["records"]["1"]["offset"] == 0


def test_budget_expired_before_request_does_not_call_source(monkeypatch):
    target = connector(monkeypatch)

    async def unexpected(*args):
        raise AssertionError("Source request must not start")

    monkeypatch.setattr(target, "_json", unexpected)
    with pytest.raises(HistoryBudgetDeferred):
        asyncio.run(target._history_json(None, "https://example.test", time.monotonic() - 1))


def test_metadata_phase_deferral_is_pending_with_no_failure_diagnostic(monkeypatch):
    target = AzureDevOpsConnector(AzureDevOpsSettings(
        organization="synthetic", history_request_timeout_seconds=1, history_timeout_seconds=.01), 30)
    target.verified_cache_scope = "verified"

    async def slow(client, method, url):
        await asyncio.sleep(1)

    monkeypatch.setattr(target, "_json", slow)
    asyncio.run(target._capture_history(None, [item()]))
    progress = target._history_coverage
    assert progress["metadata_status"] == "pending" and progress["budget_deferred"] > 0
    assert progress["metadata_diagnostic"] == {} and progress["failed"] == 0


def test_slow_catalogue_reserves_item_history_budget_on_every_refresh(monkeypatch):
    target = AzureDevOpsConnector(AzureDevOpsSettings(
        organization="synthetic", history_request_timeout_seconds=1, history_timeout_seconds=.2), 30)
    target.verified_cache_scope = "verified"
    cancelled = []
    update_reads = []

    async def read(client, method, url):
        if "/fields?" in url:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled.append(url)
                raise
        update_reads.append(url)
        return {"value": [update()]}, {}

    monkeypatch.setattr(target, "_json", read)
    events = asyncio.run(target._capture_history(None, [item()]))
    assert [event.source_update_id for event in events] == [1]
    assert len(update_reads) == len(cancelled) == 1
    assert target._history_coverage["metadata_status"] == "pending"
    assert target._history_coverage["failed"] == 0
    assert target._history_coverage["captured"] == 1
    assert target._history_coverage["metadata_diagnostic"] == {}
    assert asyncio.run(target._capture_history(None, [item()])) == []
    assert len(update_reads) == len(cancelled) == 2


def test_timestamp_gap_rescan_retries_the_failed_page_after_restart(monkeypatch):
    target = connector(monkeypatch, history_page_size=2)
    rows = [{"id": 1, "fields": {}, "revisedDate": "9999-01-01T00:00:00Z"},
            {**update(), "id": 2}, {**update(), "id": 3}]
    calls = []

    async def read(client, method, url):
        from urllib.parse import parse_qs, urlsplit

        offset = int(parse_qs(urlsplit(url).query)["$skip"][0])
        calls.append(offset)
        return {"value": rows[offset:offset + 2]}, {}

    monkeypatch.setattr(target, "_json", read)
    asyncio.run(target._capture_history(None, [item()]))
    asyncio.run(target._capture_history(None, [item()]))
    assert target._history.records["1"]["status"] == "unavailable"

    async def failed(client, method, url):
        raise status_error(503)

    monkeypatch.setattr(target, "_json", failed)
    asyncio.run(target._capture_history(None, [item()]))
    assert target._history.records["1"]["offset"] == 3
    assert target._history.records["1"]["retry_offset"] == 0
    reopened = connector(monkeypatch, history_page_size=2)
    reopened.load_state(target.checkpoint())
    reopened._ensure_history().records["1"]["retry_at"] = ""
    rows[0] = update()
    monkeypatch.setattr(reopened, "_json", read)
    events = asyncio.run(reopened._capture_history(None, [item()]))
    assert calls[-1] == 0
    assert [event.source_update_id for event in events] == [1]


def test_real_access_denial_preserves_http_code_without_response_body():
    target = AzureDevOpsConnector(AzureDevOpsSettings(organization="synthetic"), 30)

    async def read():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(403, content=b"private body", request=request))) as client:
            try:
                await target._json(client, "GET", "https://example.test")
            except Exception as exc:
                return target._history_failure(exc)

    assert asyncio.run(read()) == ("HTTP_403", "inaccessible")


def test_metadata_and_inventory_diagnostics_are_sanitized_and_persisted(monkeypatch):
    target = AzureDevOpsConnector(AzureDevOpsSettings(organization="synthetic"), 30)
    target.verified_cache_scope = "verified"

    async def denied(client, method, url):
        raise status_error(403)

    def inventory(scope):
        raise OSError("private path and content must not persist")

    target.history_inventory_loader = inventory
    monkeypatch.setattr(target, "_json", denied)
    asyncio.run(target._capture_history(None, [item()]))
    progress = target._history_coverage
    assert progress["metadata_diagnostic"]["error_code"] == "HTTP_403"
    assert progress["metadata_diagnostic"]["last_failure_at"]
    assert progress["inventory_diagnostic"]["error_code"] == "OSError"
    serialized = json.dumps(target.checkpoint())
    assert "private" not in serialized and "must-not-persist" not in serialized
    saved = AdoHistory("verified", target.checkpoint())
    assert saved.metadata_diagnostic == progress["metadata_diagnostic"]
    assert saved.inventory_diagnostic == progress["inventory_diagnostic"]


def test_cooldown_does_not_starve_other_items_or_lose_legacy_state():
    history = AdoHistory("scope", {"version": 1, "scope": "scope", "retention_days": 60,
                                   "records": {"1": {"offset": 9, "status": "failed", "error_code": "HTTP_503"}}})
    history.prepare({"1", "2", "3"}, NOW - timedelta(days=60))
    history.fail("1", "HTTP_404_UNCONFIRMED", category="inaccessible", retry_seconds=1800)
    history.cursor = "1"
    assert history.batch(2) == ["2", "3"]
    assert history.records["1"]["offset"] == 9
    assert AdoHistory("scope", history.checkpoint()).batch(3) == ["2", "3"]


def test_retry_after_http_date_is_respected_and_malformed_values_are_ignored():
    from email.utils import format_datetime

    future = utc_now() + timedelta(minutes=10)
    delay = AzureDevOpsConnector._history_retry_after(status_error(429, headers={"Retry-After": format_datetime(future)}))
    assert 590 < delay <= 600
    assert AzureDevOpsConnector._history_retry_after(status_error(429, headers={"Retry-After": "NaN"})) == 0
