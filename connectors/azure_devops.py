from __future__ import annotations

import asyncio
import html
import json
import math
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

from activity_details import ado_actor, ado_changes
from config import AzureDevOpsSettings, source_scope
from models import Activity, ConnectorHealth, ConnectorResult, WorkItem, utc_now
from safety import assert_ado_read_operation, safe_error
from .base import CommandSpec, ConnectorFailure, display_name, local_cli_bridge, parse_datetime, parse_json_output, run_command, unique_strings
from .ado_history import AdoHistory


ADO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"
API_VERSION = "7.1"
# Optional history reads should not repeatedly spend the foreground refresh
# budget on work that the source currently cannot expose. They remain retryable.
HISTORY_UNAVAILABLE_RETRY_SECONDS = 1800
HISTORY_TRANSIENT_RETRY_SECONDS = 30


class HistoryBudgetDeferred(Exception):
    """The shared history phase ended without establishing a failed read."""


class AzureDevOpsConnector:
    name = "azure_devops"

    def __init__(self, settings: AzureDevOpsSettings, timeout_seconds: int, retention_days: int = 60):
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self.retention_days = retention_days
        self._saved_state: dict = {}
        self.state_loader: Callable[[str], dict] | None = None
        self.history_inventory_loader: Callable[[str], list[dict]] | None = None
        self._history_coverage: dict = {}
        self.scope_verified: Callable[[str], None] | None = None
        self.verified_cache_scope: str | None = None

    def load_state(self, state: dict | None) -> None:
        self._saved_state = state or {}
        if hasattr(self, "_history"):
            del self._history

    def checkpoint(self) -> dict:
        return self._history.checkpoint() if hasattr(self, "_history") else {}

    def _ensure_history(self) -> AdoHistory:
        scope = self.verified_cache_scope or source_scope(
            self.name, self.settings.organization, self.settings.expected_account
        )
        if not hasattr(self, "_history") or self._history.scope != scope:
            self._history = AdoHistory(scope, self._saved_state)
        return self._history

    async def refresh(self) -> ConnectorResult:
        self.verified_cache_scope = None
        self._history_coverage = {}
        attempted = utc_now()
        if not self.settings.enabled:
            return self._result("disabled", "Azure DevOps connector is disabled", attempted)
        try:
            executable = self._find_cli()
            account = await asyncio.to_thread(self._authenticate, executable)
            self.verified_cache_scope = source_scope(self.name, self.settings.organization, account)
            if self.scope_verified is not None:
                self.scope_verified(self.verified_cache_scope)
            # Always reload committed state. If a previous snapshot was rejected
            # or cancelled, its in-memory progress must be replayed safely.
            if self.state_loader is not None:
                self.load_state(self.state_loader(self.verified_cache_scope))
            token = await asyncio.to_thread(self._access_token, executable)
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as client:
                identity = await self._connection_identity(client)
                identity_name = (
                    identity.get("properties", {}).get("Account", {}).get("$value")
                    or identity.get("providerDisplayName")
                    or account
                )
                work_items, activities, projects = await self._work_items(client, identity)
                pr_items, pr_activity, pr_partial = await self._pull_requests(client, identity)
                work_items.extend(pr_items)
                activities.extend(pr_activity)

            message = "Azure DevOps refreshed successfully"
            history_partial = bool(self._history_coverage.get("incomplete"))
            state = "partial" if pr_partial or self._work_partial or history_partial else "ok"
            if pr_partial or self._work_partial:
                message = "Azure DevOps refreshed with incomplete work-item or PR coverage"
            elif history_partial:
                if (self._history_coverage.get('failed') or self._history_coverage.get('inventory_failed')
                        or self._history_coverage.get('metadata_status') == 'failed'):
                    message = "Azure DevOps work refreshed; some change details could not be read and will retry"
                elif self._history_coverage.get('deleted') or self._history_coverage.get('inaccessible'):
                    message = "Azure DevOps work refreshed; some tracked items are deleted or inaccessible; availability will be rechecked"
                elif self._history_coverage.get('unavailable'):
                    message = "Azure DevOps work refreshed; some update timestamps are unavailable"
                else:
                    message = "Azure DevOps work refreshed; change details continue in saved batches"
            return ConnectorResult(
                connector=self.name,
                cache_scope=self.verified_cache_scope,
                work_items=work_items,
                activities=activities,
                checkpoint=self.checkpoint(),
                health=ConnectorHealth(
                    connector=self.name,
                    state=state,
                    message=message,
                    last_attempt_at=attempted,
                    last_success_at=utc_now(),
                    coverage={
                        "organization": self.settings.organization,
                        "authenticated_identity": str(identity_name or account),
                        "work_items": len([item for item in work_items if item.source == "ado"]),
                        "pull_requests": len(pr_items),
                        "projects_considered": len(projects),
                        "pull_request_query_scope": "organization",
                        "historical_coverage": "Partial: prior assignment queried; comments limited to discovered items; subscriptions not collected",
                        "work_query_incomplete": self._work_partial,
                        "pull_request_query_incomplete": pr_partial,
                        "history_progress": self._history_coverage,
                    },
                ),
            )
        except ConnectorFailure as exc:
            return self._result(
                "auth_required" if exc.auth_required else "error",
                str(exc),
                attempted,
                exc.code,
                cache_scope=self.verified_cache_scope,
            )
        except (httpx.HTTPError, ValueError, OSError) as exc:
            return self._result(
                "error", "Azure DevOps read failed", attempted, exc.__class__.__name__, exc,
                cache_scope=self.verified_cache_scope,
            )

    def _result(
        self,
        state: str,
        message: str,
        attempted: datetime,
        code: str | None = None,
        diagnostic: Exception | None = None,
        *,
        cache_scope: str | None = None,
    ) -> ConnectorResult:
        coverage = {"organization": self.settings.organization}
        if diagnostic:
            coverage["diagnostic"] = safe_error(diagnostic)
        return ConnectorResult(
            connector=self.name,
            cache_scope=cache_scope,
            health=ConnectorHealth(
                connector=self.name,
                state=state,
                message=message,
                last_attempt_at=attempted,
                error_code=code,
                coverage=coverage,
            ),
        )

    def _find_cli(self) -> str | CommandSpec:
        configured = self.settings.cli_path.strip()
        project_local = Path(__file__).resolve().parents[1] / ".tools" / "azure-cli" / "bin" / "az.cmd"
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        candidates = [
            configured,
            shutil.which("az"),
            str(project_local),
            str(local_app_data / "MuchADOAboutJira" / "tools" / "azure-cli" / "bin" / "az.cmd"),
        ]
        executable = next((value for value in candidates if value and Path(value).exists()), None)
        if executable:
            return executable
        return local_cli_bridge("az")

    def _authenticate(self, executable: str | CommandSpec) -> str:
        account_result = run_command(
            executable, ["account", "show", "--output", "json", "--only-show-errors"], self.timeout_seconds
        )
        if account_result.returncode:
            raise ConnectorFailure(
                "AZ_LOGIN_REQUIRED",
                "Azure CLI sign-in is required; run az login interactively",
                auth_required=True,
            )
        account_data = parse_json_output(account_result.stdout)
        account = str((account_data.get("user") or {}).get("name") or "")
        self._verify_identity(account)
        return account

    def _access_token(self, executable: str | CommandSpec) -> str:
        token_result = run_command(
            executable,
            [
                "account",
                "get-access-token",
                "--resource",
                ADO_RESOURCE_ID,
                "--query",
                "accessToken",
                "--output",
                "tsv",
                "--only-show-errors",
            ],
            self.timeout_seconds,
        )
        token = token_result.stdout.strip()
        if token_result.returncode or not token:
            raise ConnectorFailure(
                "AZ_TOKEN_UNAVAILABLE",
                "Azure CLI could not obtain a delegated Azure DevOps token; sign in again",
                auth_required=True,
            )
        return token

    def _verify_identity(self, actual: str) -> None:
        if not actual.strip():
            raise ConnectorFailure(
                "IDENTITY_UNAVAILABLE",
                "Azure CLI did not expose an account",
                auth_required=True,
            )
        expected = self.settings.expected_account.strip().lower()
        if expected and actual.strip().lower() != expected:
            raise ConnectorFailure(
                "IDENTITY_MISMATCH",
                f"Azure CLI identity does not match the configured corporate account ({actual})",
                auth_required=True,
            )

    @property
    def base_url(self) -> str:
        return f"https://dev.azure.com/{quote(self.settings.organization)}"

    async def _json(
        self, client: httpx.AsyncClient, method: str, url: str, *, body: dict | None = None
    ) -> tuple[dict[str, Any], httpx.Headers]:
        assert_ado_read_operation(method, url)
        response = await client.request(method, url, json=body)
        if response.status_code in {401, 403}:
            failure = ConnectorFailure(
                "ADO_ACCESS_DENIED",
                "Azure DevOps authentication or organization access is required",
                auth_required=True,
            )
            failure.http_status = response.status_code
            raise failure
        response.raise_for_status()
        payload = response.json()
        return (payload if isinstance(payload, dict) else {"value": payload}), response.headers

    async def _connection_identity(self, client: httpx.AsyncClient) -> dict[str, Any]:
        url = f"{self.base_url}/_apis/connectionData?connectOptions=1&lastChangeId=-1&lastChangeId64=-1"
        data, _ = await self._json(client, "GET", url)
        return data.get("authenticatedUser") or {}

    async def _projects(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        continuation: str | None = None
        while True:
            suffix = f"&continuationToken={quote(continuation)}" if continuation else ""
            data, headers = await self._json(
                client,
                "GET",
                f"{self.base_url}/_apis/projects?stateFilter=wellFormed&$top=100&api-version={API_VERSION}{suffix}",
            )
            projects.extend(data.get("value") or [])
            continuation = headers.get("x-ms-continuationtoken")
            if not continuation:
                break
        return projects

    async def _wiql(self, client: httpx.AsyncClient, where: str) -> list[int]:
        query = f"SELECT [System.Id] FROM WorkItems WHERE {where} ORDER BY [System.ChangedDate] DESC"
        data, _ = await self._json(
            client,
            "POST",
            f"{self.base_url}/_apis/wit/wiql?$top={self.settings.max_work_items}&api-version={API_VERSION}",
            body={"query": query},
        )
        return [int(item["id"]) for item in data.get("workItems") or [] if item.get("id")]

    async def _hydrate(self, client: httpx.AsyncClient, ids: list[int]) -> list[dict[str, Any]]:
        fields = ",".join(
            [
                "System.Id",
                "System.TeamProject",
                "System.WorkItemType",
                "System.Title",
                "System.State",
                "System.AssignedTo",
                "System.CreatedBy",
                "System.CreatedDate",
                "System.ChangedDate",
                "System.Tags",
                "System.CommentCount",
                "Microsoft.VSTS.Common.Priority",
            ]
        )
        records: list[dict[str, Any]] = []
        for start in range(0, len(ids), 200):
            batch = ",".join(str(value) for value in ids[start : start + 200])
            data, _ = await self._json(
                client,
                "GET",
                f"{self.base_url}/_apis/wit/workitems?ids={batch}&fields={quote(fields, safe=',')}&api-version={API_VERSION}",
            )
            records.extend(data.get("value") or [])
        return records

    async def _state_categories(
        self, client: httpx.AsyncClient, records: list[dict[str, Any]]
    ) -> dict[tuple[str, str, str], str]:
        pairs = {
            (str(item.get("fields", {}).get("System.TeamProject") or ""), str(item.get("fields", {}).get("System.WorkItemType") or ""))
            for item in records
        }
        categories: dict[tuple[str, str, str], str] = {}
        for project, item_type in pairs:
            if not project or not item_type:
                continue
            try:
                data, _ = await self._json(
                    client,
                    "GET",
                    f"{self.base_url}/{quote(project)}/_apis/wit/workitemtypes/{quote(item_type)}/states?api-version={API_VERSION}",
                )
            except (httpx.HTTPError, ConnectorFailure):
                continue
            for state in data.get("value") or []:
                key = (project, item_type, str(state.get("name") or ""))
                categories[key] = self._map_category(str(state.get("category") or ""))
        return categories

    @staticmethod
    def _map_category(value: str) -> str:
        lowered = value.lower()
        if lowered in {"completed", "removed"}:
            return "done"
        if lowered == "inprogress":
            return "in_progress"
        if lowered == "proposed":
            return "todo"
        return "other"

    async def _work_items(
        self, client: httpx.AsyncClient, identity: dict[str, Any]
    ) -> tuple[list[WorkItem], list[Activity], list[dict[str, Any]]]:
        self._work_partial = False
        async def past_assignments():
            try:
                return await self._wiql(client, 'EVER [System.AssignedTo] = @Me')
            except Exception:
                self._work_partial = True
                return []
        assigned_ids, authored_ids, projects, previous_ids = await asyncio.gather(
            self._wiql(client, "[System.AssignedTo] = @Me"),
            self._wiql(client, "[System.CreatedBy] = @Me"),
            self._projects(client),
            past_assignments(),
        )
        combined_ids = list(dict.fromkeys(assigned_ids + authored_ids + previous_ids))
        if len(combined_ids) > self.settings.max_work_items or any(len(ids) >= self.settings.max_work_items for ids in (assigned_ids, authored_ids, previous_ids)):
            self._work_partial = True
        all_ids = combined_ids[: self.settings.max_work_items]
        previous_set = set(previous_ids)
        records = await self._hydrate(client, all_ids)
        categories = await self._state_categories(client, records)
        assigned_set, authored_set = set(assigned_ids), set(authored_ids)
        self_id = str(identity.get("id") or "").lower()
        items: list[WorkItem] = []
        activities: list[Activity] = []
        candidates: list[tuple[WorkItem, dict[str, Any]]] = []

        for record in records:
            fields = record.get("fields") or {}
            work_id = int(record.get("id") or fields.get("System.Id"))
            project = str(fields.get("System.TeamProject") or "Unknown project")
            item_type = str(fields.get("System.WorkItemType") or "Work item")
            state = str(fields.get("System.State") or "Unknown")
            category = categories.get((project, item_type, state), "other")
            if category == "other":
                state_lower = state.lower()
                if state_lower in {"closed", "done", "resolved", "removed", "completed"}:
                    category = "done"
                elif any(word in state_lower for word in ("active", "progress", "committed")):
                    category = "in_progress"
                elif any(word in state_lower for word in ("new", "ready", "proposed", "to do")):
                    category = "todo"
            tags = str(fields.get("System.Tags") or "")
            if category != "done" and ("blocked" in state.lower() or "blocked" in tags.lower()):
                category = "blocked"
            reasons: list[str] = []
            if work_id in assigned_set:
                reasons.append("assigned")
            if work_id in previous_set and work_id not in assigned_set:
                reasons.append("previously_assigned")
            if work_id in authored_set:
                reasons.extend(["author", "waiting"])
            updated = parse_datetime(fields.get("System.ChangedDate")) or utc_now()
            title = str(fields.get("System.Title") or f"Work item {work_id}")
            item = WorkItem(
                id=f"ado:workitem:{work_id}",
                source="ado",
                source_type=item_type.lower().replace(" ", "_"),
                project=project,
                key=str(work_id),
                title=title,
                url=f"{self.base_url}/{quote(project)}/_workitems/edit/{work_id}",
                status=state,
                status_category=category,
                priority=str(fields.get("Microsoft.VSTS.Common.Priority") or "") or None,
                assigned_to=display_name(fields.get("System.AssignedTo")),
                author=display_name(fields.get("System.CreatedBy")),
                updated_at=updated,
                created_at=parse_datetime(fields.get("System.CreatedDate")),
                actionable=category == "blocked",
                reasons=unique_strings(reasons),
                metadata={"work_item_type": item_type, "tags": tags},
            )
            items.append(item)
            activities.append(
                Activity(
                    id=f"ado:workitem:{work_id}:updated:{updated.isoformat()}",
                    source="ado",
                    event_type="updated",
                    item_id=item.id,
                    item_key=item.key,
                    item_title=title,
                    timestamp=updated,
                    summary=f"{item_type} updated",
                    detail_status="pending",
                    source_revision=record.get("rev"),
                    url=item.url,
                    reasons=list(item.reasons),
                )
            )
            cutoff = utc_now() - timedelta(days=self.settings.mention_reply_days)
            if updated >= cutoff and int(fields.get("System.CommentCount") or 0) > 0:
                candidates.append((item, record))

        activities.extend(await self._capture_history(client, items))
        # The placeholder has its own explicit outcome until an authoritative
        # event timestamp lets Store link it to a canonical update ID.
        history = self._ensure_history()
        for activity in activities:
            if activity.source_update_id is not None or activity.event_type != "updated":
                continue
            progress = history.records.get(activity.item_key, {})
            status = progress.get("status", "pending")
            if status == "failed":
                activity.detail_status = "failed"
            elif status in {"deleted", "inaccessible"}:
                activity.detail_status = "unavailable"
            elif status in {"captured", "unavailable"} and progress.get("latest_changed_date") != activity.timestamp.isoformat():
                activity.detail_status = "unavailable"
        candidates = candidates[: self.settings.max_comment_candidates]
        semaphore = asyncio.Semaphore(6)

        async def enrich(item: WorkItem, record: dict[str, Any]):
            async with semaphore:
                return await self._work_item_comments(client, item, self_id)

        enriched = await asyncio.gather(
            *(enrich(item, record) for item, record in candidates), return_exceptions=True
        )
        for result in enriched:
            if isinstance(result, tuple):
                item_id, reasons, new_activity = result
                target = next((item for item in items if item.id == item_id), None)
                if target:
                    target.reasons = unique_strings(target.reasons + reasons)
                    target.actionable = target.actionable or bool({"mentioned", "replied"} & set(reasons))
                activities.extend(new_activity)
        return items, activities, projects

    async def _history_json(self, client: httpx.AsyncClient, url: str, deadline: float) -> dict:
        """Retry transient history reads within the shared, bounded budget."""
        last_failure = None
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if last_failure is not None:
                    raise last_failure
                raise HistoryBudgetDeferred()
            request_timeout = min(float(self.timeout_seconds), float(self.settings.history_request_timeout_seconds))
            phase_limited = remaining < request_timeout
            try:
                data, _ = await asyncio.wait_for(
                    self._json(client, "GET", url), min(request_timeout, remaining)
                )
                return data
            except (httpx.HTTPError, TimeoutError) as exc:
                # A request cut short only by the shared batch deadline has not
                # reached its own timeout. Continue it next batch as normal work.
                if isinstance(exc, TimeoutError) and phase_limited:
                    if last_failure is not None:
                        raise last_failure
                    raise HistoryBudgetDeferred() from None
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                retryable = status is None or status in {408, 429} or status >= 500
                if not retryable or attempt:
                    raise
                last_failure = exc
                delay = max(0.1, self._history_retry_after(exc))
                if deadline - time.monotonic() <= delay:
                    raise
                await asyncio.sleep(delay)
        raise RuntimeError("Unreachable history retry")

    @staticmethod
    def _history_retry_after(exc: Exception) -> float:
        if not isinstance(exc, httpx.HTTPStatusError):
            return 0
        value = exc.response.headers.get("Retry-After", "")
        try:
            seconds = float(value)
            return min(86400.0, max(0.0, seconds)) if math.isfinite(seconds) else 0
        except ValueError:
            try:
                stamp = parsedate_to_datetime(value)
                return min(86400.0, max(0.0, (stamp - utc_now()).total_seconds()))
            except (ValueError, TypeError, OverflowError):
                return 0

    @staticmethod
    def _history_failure(exc: Exception) -> tuple[str, str]:
        if isinstance(exc, ConnectorFailure):
            status = getattr(exc, "http_status", None)
            if status in {401, 403}:
                return f"HTTP_{status}", "inaccessible"
            return exc.code, "inaccessible" if exc.code == "ADO_ACCESS_DENIED" else "read_error"
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            category = ("transient" if status in {408, 429} or status >= 500
                        else "inaccessible" if status in {401, 403, 404, 410} else "read_error")
            return f"HTTP_{status}", category
        if isinstance(exc, (TimeoutError, httpx.TransportError)):
            return type(exc).__name__, "transient"
        return type(exc).__name__, "invalid_response" if isinstance(exc, (ValueError, TypeError)) else "read_error"

    def _history_retry_seconds(self, category: str, failures: int, exc: Exception) -> float:
        if category in {"deleted", "inaccessible"}:
            return HISTORY_UNAVAILABLE_RETRY_SECONDS
        return max(self._history_retry_after(exc), min(900, HISTORY_TRANSIENT_RETRY_SECONDS * 2 ** min(failures, 5)))

    async def _history_deleted(self, client: httpx.AsyncClient, key: str, deadline: float) -> bool:
        """Only a successful matching recycle-bin response establishes deletion."""
        try:
            data = await self._history_json(
                client, f"{self.base_url}/_apis/wit/recyclebin/{key}?api-version={API_VERSION}", deadline
            )
            identifier = data.get("id")
            return not isinstance(identifier, bool) and str(identifier) == key
        except (httpx.HTTPError, ConnectorFailure, HistoryBudgetDeferred, TimeoutError, ValueError, TypeError):
            return False

    async def _history_metadata(self, client: httpx.AsyncClient, deadline: float) -> str:
        history = self._ensure_history()
        updated = parse_datetime(history.metadata_at)
        if history.field_metadata and updated and updated >= utc_now() - timedelta(days=1):
            return "cached"
        if not AdoHistory.due(history.metadata_diagnostic):
            return "failed"
        try:
            data = await self._history_json(
                client, f"{self.base_url}/_apis/wit/fields?api-version={API_VERSION}", deadline
            )
            values = data.get("value")
            if not isinstance(values, list):
                raise ValueError("ADO fields metadata was not a list")
            metadata = {}
            for field in values:
                if not isinstance(field, dict) or not isinstance(field.get("referenceName"), str):
                    continue
                metadata[field["referenceName"]] = {
                    "name": field.get("name"),
                    "type": "identity" if field.get("isIdentity") else field.get("type"),
                }
            sanitized = AdoHistory.sanitize_metadata(metadata)
            if sanitized != history.field_metadata:
                # Revisit stable update IDs after a catalogue recovery/change so
                # earlier fallback formatting can be enriched in place.
                for record in history.records.values():
                    status = record["status"] if record["status"] in {"deleted", "inaccessible"} else "pending"
                    record.update(offset=0, retry_offset=None, seen={}, status=status, undated=[], latest_changed_date="")
            history.field_metadata = sanitized
            history.metadata_at = utc_now().isoformat()
            history.metadata_diagnostic = AdoHistory.sanitize_diagnostic({})
            return "captured"
        except HistoryBudgetDeferred:
            return "pending"
        except (httpx.HTTPError, ConnectorFailure, TimeoutError, ValueError, TypeError) as exc:
            # Parsing can safely derive field labels and body-only markers even
            # if the organization denies its field catalogue.
            code, category = self._history_failure(exc)
            failures = history.metadata_diagnostic.get("consecutive_failures", 0)
            now = utc_now()
            history.metadata_diagnostic = AdoHistory.sanitize_diagnostic({
                "error_code": code, "failure_category": category,
                "last_failure_at": now.isoformat(),
                "retry_at": (now + timedelta(seconds=self._history_retry_seconds(category, failures, exc))).isoformat(),
                "consecutive_failures": failures + 1,
            })
            return "failed"

    @staticmethod
    def _history_target(value: WorkItem | dict) -> dict | None:
        data = value.model_dump() if isinstance(value, WorkItem) else value
        if not isinstance(data, dict):
            return None
        key = str(data.get("key") or str(data.get("id") or "").rsplit(":", 1)[-1])
        if not key.isdigit() or data.get("source", "ado") != "ado":
            return None
        updated = parse_datetime(data.get("updated_at"))
        if not updated:
            return None
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        latest_activity = parse_datetime(metadata.get("latest_activity_at"))
        if latest_activity and latest_activity.tzinfo is None:
            latest_activity = latest_activity.replace(tzinfo=timezone.utc)
        return {
            "id": f"ado:workitem:{key}", "key": key,
            "title": str(data.get("title") or f"Work item {key}"),
            "url": str(data.get("url") or ""), "updated_at": updated,
            "history_at": max(updated, latest_activity) if latest_activity else updated,
            "snapshot_only": bool(metadata.get("snapshot_only")),
            "reasons": data.get("reasons") if isinstance(data.get("reasons"), list) else [],
        }

    async def _capture_history(
        self, client: httpx.AsyncClient, items: list[WorkItem | dict], *, include_inventory: bool = True
    ) -> list[Activity]:
        """Capture a fair page of each selected item, including retained items.

        This can also be called with a cached minimal inventory for a targeted
        backfill. Its checkpoint must be accepted atomically with its events.
        """
        history = self._ensure_history()
        history.set_retention_days(self.retention_days)
        cutoff = utc_now() - timedelta(days=self.retention_days)
        candidates = list(items)
        visible_keys = {target["key"] for value in items
                        if (target := self._history_target(value)) and not target["snapshot_only"]}
        inventory_failed = False
        if include_inventory and self.history_inventory_loader is not None and self.verified_cache_scope:
            try:
                candidates = list(self.history_inventory_loader(self.verified_cache_scope)) + candidates
                history.inventory_diagnostic = AdoHistory.sanitize_diagnostic({})
            except (ValueError, OSError, TypeError) as exc:
                inventory_failed = True
                history.inventory_diagnostic = AdoHistory.sanitize_diagnostic({
                    "error_code": type(exc).__name__, "failure_category": "read_error",
                    "last_failure_at": utc_now().isoformat(),
                    "consecutive_failures": history.inventory_diagnostic.get("consecutive_failures", 0) + 1,
                })
        targets = {}
        for value in candidates:
            target = self._history_target(value)
            if target and (not target["snapshot_only"] or target["history_at"] >= cutoff):
                targets[target["key"]] = target
        # An inventory outage must not discard cursors for retained items.
        history.prepare(set(targets) | (set(history.records) if inventory_failed else set()), cutoff)
        for key, target in targets.items():
            history.observe(key, target["updated_at"], currently_visible=key in visible_keys)
        deadline = time.monotonic() + self.settings.history_timeout_seconds
        # The catalogue is optional: fallback labels still allow update pages
        # to be captured. Reserve at least half of this phase for those pages so
        # a slow catalogue cannot prevent history progress on every refresh.
        metadata_started = time.monotonic()
        metadata_deadline = metadata_started + max(0, deadline - metadata_started) / 2
        metadata_status = await self._history_metadata(client, metadata_deadline) if targets else "not_needed"
        events: list[Activity] = []
        pages = attempted = 0
        budget_deferred = int(metadata_status == "pending")
        page_size = self.settings.history_page_size
        for key in history.batch(self.settings.history_batch_size):
            if time.monotonic() >= deadline:
                budget_deferred += 1
                break
            target = targets.get(key)
            if target is None:
                continue
            offset = history.start(key)
            attempted += 1
            try:
                data = await self._history_json(
                    client, f"{self.base_url}/_apis/wit/workItems/{key}/updates?"
                    f"$top={page_size}&$skip={offset}&api-version={API_VERSION}", deadline
                )
                rows = data.get("value")
                if not isinstance(rows, list):
                    raise ValueError("ADO update page was not a list")
                page_events: dict[int, Activity] = {}
                seen: dict[str, str] = {}
                undated: set[int] = set()
                resolved: set[int] = set()
                latest_changed_date = ""
                for update in rows:
                    if not isinstance(update, dict):
                        raise ValueError("ADO update row was not an object")
                    identifier = update.get("id")
                    if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1:
                        raise ValueError("ADO update row lacks a stable update ID")
                    fields = update.get("fields") or {}
                    if not isinstance(fields, dict):
                        raise ValueError("ADO update fields were not an object")
                    delta = fields.get("System.ChangedDate") or {}
                    changed_at = parse_datetime(delta.get("newValue")) if isinstance(delta, dict) else None
                    revised_at = parse_datetime(update.get("revisedDate"))
                    now = utc_now()
                    # ADO uses 9999-01-01 as a sentinel revisedDate. It cannot
                    # establish a historical event time or a placeholder alias.
                    if changed_at and (changed_at.tzinfo is None or changed_at > now + timedelta(days=1)):
                        changed_at = None
                    if revised_at and (revised_at.tzinfo is None or revised_at > now + timedelta(days=1)):
                        revised_at = None
                    changes = ado_changes(update, history.field_metadata)
                    # revisedDate on field revisions may be the boundary of the
                    # next revision. Only relation-only updates without fields
                    # can use it as a fallback time.
                    if not changed_at and fields:
                        revised_at = None
                    timestamp = changed_at or revised_at
                    if changed_at:
                        latest_changed_date = max(latest_changed_date, changed_at.isoformat())
                    if timestamp:
                        resolved.add(identifier)
                    if timestamp and timestamp < cutoff:
                        continue
                    if timestamp is None:
                        undated.add(identifier)
                        continue
                    seen[str(identifier)] = timestamp.isoformat()
                    if str(identifier) in history.records[key]["seen"] or identifier in page_events:
                        continue
                    detail_status = "captured" if changes else "administrative"
                    revision = update.get("rev")
                    page_events[identifier] = Activity(
                        id=f"ado:workitem:{key}:update:{identifier}", source="ado", event_type="updated",
                        item_id=target["id"], item_key=key, item_title=target["title"], timestamp=timestamp,
                        actor=ado_actor(update.get("revisedBy")),
                        summary="; ".join(changes) if changes else "Administrative update; no user-facing field changes",
                        changes=changes, detail_source="ADO update history", detail_status=detail_status,
                        source_update_id=identifier,
                        source_revision=revision if isinstance(revision, int) and not isinstance(revision, bool) else None,
                        legacy_activity_id=f"ado:workitem:{key}:updated:{changed_at.isoformat()}" if changed_at else None,
                        url=target["url"], reasons=target["reasons"],
                    )
                # No page progress is accepted before every row is valid and
                # every event is normalized. Store commits this state+events.
                history.accept(key, offset, len(rows), page_size, seen, latest_changed_date, undated, resolved)
                events.extend(page_events.values())
                pages += 1
            except HistoryBudgetDeferred:
                budget_deferred += 1
                break
            except (httpx.HTTPError, ConnectorFailure, TimeoutError, ValueError, TypeError) as exc:
                code, category = self._history_failure(exc)
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                    if await self._history_deleted(client, key, deadline):
                        code, category = "ADO_RECYCLE_BIN_CONFIRMED", "deleted"
                    else:
                        code = "HTTP_404_UNCONFIRMED"
                retry = self._history_retry_seconds(category, history.records[key].get("consecutive_failures", 0), exc)
                history.fail(key, code, category=category, retry_seconds=retry, retry_offset=offset)
        counts = {status: sum(record["status"] == status for record in history.records.values())
                  for status in ("captured", "pending", "failed", "unavailable", "deleted", "inaccessible")}
        remaining = counts["pending"] + counts["failed"]
        diagnostics = [{"item_key": key, **AdoHistory.sanitize_diagnostic(record)}
                       for key, record in sorted(history.records.items())
                       if record["status"] in {"failed", "deleted", "inaccessible"}]
        self._history_coverage = {
            "total": len(history.records), "checked": counts["captured"] + counts["unavailable"] + counts["deleted"] + counts["inaccessible"],
            "remaining": remaining, **counts, "pages": pages, "attempted": attempted, "events": len(events),
            "retention_days": self.retention_days, "metadata_status": metadata_status,
            "inventory_failed": inventory_failed,
            "inventory_diagnostic": history.inventory_diagnostic if inventory_failed else {},
            "metadata_diagnostic": history.metadata_diagnostic if metadata_status == "failed" else {},
            "failure_diagnostics": diagnostics[:50],
            "failure_diagnostics_truncated": len(diagnostics) > 50,
            "cooling_down": sum(not AdoHistory.due(record) for record in history.records.values()),
            "budget_deferred": budget_deferred,
            "unavailable_timestamps": sum(len(record["undated"]) for record in history.records.values()),
            "incomplete": bool(remaining or counts["unavailable"] or counts["deleted"] or counts["inaccessible"]
                               or metadata_status in {"failed", "pending"} or inventory_failed),
        }
        return events

    async def _work_item_comments(
        self, client: httpx.AsyncClient, item: WorkItem, self_id: str
    ) -> tuple[str, list[str], list[Activity]]:
        data, _ = await self._json(
            client,
            "GET",
            f"{self.base_url}/{quote(item.project)}/_apis/wit/workItems/{item.key}/comments?$top=200&api-version=7.1-preview.4",
        )
        comments = data.get("comments") or data.get("value") or []
        comments.sort(key=lambda row: str(row.get("createdDate") or row.get("modifiedDate") or ""))
        reasons: list[str] = []
        activity: list[Activity] = []
        last_self: datetime | None = None
        cutoff = utc_now() - timedelta(days=self.settings.mention_reply_days)
        for comment in comments:
            author = comment.get("createdBy") or comment.get("modifiedBy") or {}
            author_id = str(author.get("id") or "").lower()
            created = parse_datetime(comment.get("createdDate") or comment.get("modifiedDate")) or item.updated_at
            is_self = bool(self_id and author_id == self_id)
            if is_self and created >= cutoff:
                reasons.append("participant")
                last_self = created
            mentions = comment.get("mentions") or []
            mentioned = created >= cutoff and any(
                str(value.get("targetId") or value.get("id") or "").lower() == self_id
                for value in mentions
                if isinstance(value, dict)
            )
            replied = bool(created >= cutoff and last_self and not is_self and created > last_self)
            if mentioned:
                reasons.append("mentioned")
            if replied:
                reasons.append("replied")
            if mentioned or replied:
                comment_id = str(comment.get("id") or created.isoformat())
                activity.append(
                    Activity(
                        id=f"ado:workitem:{item.key}:comment:{comment_id}",
                        source="ado",
                        event_type="mention" if mentioned else "reply",
                        actor=display_name(author),
                        item_id=item.id,
                        item_key=item.key,
                        item_title=item.title,
                        timestamp=created,
                        summary=self._snippet(comment.get("text") or "New comment"),
                        url=f"{item.url}?discussionId={quote(comment_id)}",
                        reasons=["mentioned"] if mentioned else ["replied"],
                    )
                )
        return item.id, unique_strings(reasons), activity

    async def _pull_requests(
        self,
        client: httpx.AsyncClient,
        identity: dict[str, Any],
    ) -> tuple[list[WorkItem], list[Activity], bool]:
        identity_id = str(identity.get("id") or "")
        root = f"{self.base_url}/_apis/git/pullrequests"

        async def query(role: str, identity_field: str):
            return role, await self._json(
                client,
                "GET",
                f"{root}?searchCriteria.status=active&searchCriteria.{identity_field}="
                f"{quote(identity_id)}&$top=100&api-version={API_VERSION}",
            )

        scanned = await asyncio.gather(
            query("author", "creatorId"),
            query("reviewer", "reviewerId"),
            return_exceptions=True,
        )
        raw_by_id: dict[tuple[str, int], dict[str, Any]] = {}
        roles: dict[tuple[str, int], set[str]] = {}
        partial = False
        for result in scanned:
            if isinstance(result, Exception):
                partial = True
                continue
            role, (data, _) = result
            for pr in data.get("value") or []:
                key = (str((pr.get("repository") or {}).get("id") or ""), int(pr.get("pullRequestId") or 0))
                raw_by_id[key] = pr
                roles.setdefault(key, set()).add(role)

        items: list[WorkItem] = []
        activities: list[Activity] = []
        normalized = await asyncio.gather(
            *(self._normalize_pr(client, pr, roles[key], identity_id) for key, pr in raw_by_id.items()),
            return_exceptions=True,
        )
        for result in normalized:
            if isinstance(result, Exception):
                partial = True
                continue
            item, item_activity = result
            items.append(item)
            activities.extend(item_activity)
        return items, activities, partial

    async def _normalize_pr(
        self,
        client: httpx.AsyncClient,
        pr: dict[str, Any],
        roles: set[str],
        identity_id: str,
    ) -> tuple[WorkItem, list[Activity]]:
        repository = pr.get("repository") or {}
        project_data = repository.get("project") or {}
        project = str(project_data.get("name") or project_data.get("id") or "Unknown project")
        repo_name = str(repository.get("name") or "Unknown repository")
        repo_id = str(repository.get("id") or "")
        pr_id = int(pr.get("pullRequestId") or 0)
        url = f"{self.base_url}/{quote(project)}/_git/{quote(repo_name)}/pullrequest/{pr_id}"
        reviewers = pr.get("reviewers") or []
        self_review = next(
            (row for row in reviewers if str(row.get("id") or "").lower() == identity_id.lower()),
            {},
        )
        vote = int(self_review.get("vote") or 0)
        updated = parse_datetime(pr.get("creationDate")) or utc_now()
        threads_data, _ = await self._json(
            client,
            "GET",
            f"{self.base_url}/{quote(project)}/_apis/git/repositories/{quote(repo_id)}/pullRequests/{pr_id}/threads?api-version={API_VERSION}",
        )
        statuses_data, _ = await self._json(
            client,
            "GET",
            f"{self.base_url}/{quote(project)}/_apis/git/repositories/{quote(repo_id)}/pullRequests/{pr_id}/statuses?api-version={API_VERSION}",
        )
        reasons = list(roles)
        activities: list[Activity] = []
        unresolved = 0
        discussion_cutoff = utc_now() - timedelta(days=self.settings.mention_reply_days)
        for thread in threads_data.get("value") or []:
            status = str(thread.get("status") or "active").lower()
            if status in {"active", "pending", "unknown"}:
                unresolved += 1
            comments = thread.get("comments") or []
            comments.sort(key=lambda row: str(row.get("publishedDate") or row.get("lastUpdatedDate") or ""))
            last_self: datetime | None = None
            for comment in comments:
                author = comment.get("author") or {}
                is_self = str(author.get("id") or "").lower() == identity_id.lower()
                event_at = parse_datetime(comment.get("publishedDate") or comment.get("lastUpdatedDate")) or updated
                updated = max(updated, event_at)
                if is_self and event_at >= discussion_cutoff:
                    reasons.append("participant")
                    last_self = event_at
                    continue
                if event_at >= discussion_cutoff and last_self and event_at > last_self:
                    reasons.append("replied")
                    thread_id = str(thread.get("id") or "")
                    comment_id = str(comment.get("id") or event_at.isoformat())
                    activities.append(
                        Activity(
                            id=f"azure_repos:pr:{repo_id}:{pr_id}:thread:{thread_id}:comment:{comment_id}",
                            source="azure_repos",
                            event_type="reply",
                            actor=display_name(author),
                            item_id=f"azure_repos:pr:{repo_id}:{pr_id}",
                            item_key=f"PR #{pr_id}",
                            item_title=str(pr.get("title") or f"Pull request {pr_id}"),
                            timestamp=event_at,
                            summary=self._snippet(comment.get("content") or "New PR thread reply"),
                            url=f"{url}?discussionId={quote(thread_id)}",
                            reasons=["replied"],
                        )
                    )
        failed_checks = sum(
            1
            for status in statuses_data.get("value") or []
            if str(status.get("state") or "").lower() in {"failed", "error"}
        )
        requested_changes = any(int(reviewer.get("vote") or 0) < 0 for reviewer in reviewers)
        if requested_changes and "author" in roles:
            reasons.append("requested_changes")
        actionable = ("reviewer" in roles and vote == 0) or ("author" in roles and (requested_changes or failed_checks > 0))
        actionable = actionable or (unresolved > 0 and "participant" in reasons)
        item = WorkItem(
            id=f"azure_repos:pr:{repo_id}:{pr_id}",
            source="azure_repos",
            source_type="pull_request",
            project=project,
            repository=repo_name,
            key=f"PR #{pr_id}",
            title=str(pr.get("title") or f"Pull request {pr_id}"),
            url=url,
            status="Draft" if pr.get("isDraft") else "Active",
            status_category="in_progress",
            assigned_to=display_name(self_review) if "reviewer" in roles else None,
            author=display_name(pr.get("createdBy")),
            updated_at=updated,
            created_at=parse_datetime(pr.get("creationDate")),
            actionable=actionable,
            reasons=unique_strings(reasons),
            metadata={
                "reviewer_vote": vote if "reviewer" in roles else None,
                "unresolved_threads": unresolved,
                "failed_checks": failed_checks,
                "draft": bool(pr.get("isDraft")),
            },
        )
        return item, activities

    @staticmethod
    def _snippet(value: Any, limit: int = 180) -> str:
        text = html.unescape(re.sub(r"<[^>]+>", " ", str(value)))
        text = " ".join(text.split())
        return text[:limit] or "New comment"
