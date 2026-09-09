from __future__ import annotations

import asyncio
import html
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from activity_details import ado_changes
from config import AzureDevOpsSettings
from models import Activity, ConnectorHealth, ConnectorResult, WorkItem, utc_now
from safety import assert_ado_read_operation, safe_error
from .base import CommandSpec, ConnectorFailure, display_name, local_cli_bridge, parse_datetime, parse_json_output, run_command, unique_strings


ADO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"
API_VERSION = "7.1"


class AzureDevOpsConnector:
    name = "azure_devops"

    def __init__(self, settings: AzureDevOpsSettings, timeout_seconds: int):
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self._history_cache = {}

    async def refresh(self) -> ConnectorResult:
        attempted = utc_now()
        if not self.settings.enabled:
            return self._result("disabled", "Azure DevOps connector is disabled", attempted)
        try:
            executable = self._find_cli()
            account, token = await asyncio.to_thread(self._authenticate, executable)
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
            state = "partial" if pr_partial or self._work_partial else "ok"
            if pr_partial or self._work_partial:
                message = "Azure DevOps refreshed with incomplete work-item or PR coverage"
            return ConnectorResult(
                connector=self.name,
                work_items=work_items,
                activities=activities,
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
                    },
                ),
            )
        except ConnectorFailure as exc:
            return self._result(
                "auth_required" if exc.auth_required else "error",
                str(exc),
                attempted,
                exc.code,
            )
        except (httpx.HTTPError, ValueError, OSError) as exc:
            return self._result("error", "Azure DevOps read failed", attempted, exc.__class__.__name__, exc)

    def _result(
        self,
        state: str,
        message: str,
        attempted: datetime,
        code: str | None = None,
        diagnostic: Exception | None = None,
    ) -> ConnectorResult:
        coverage = {"organization": self.settings.organization}
        if diagnostic:
            coverage["diagnostic"] = safe_error(diagnostic)
        return ConnectorResult(
            connector=self.name,
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

    def _authenticate(self, executable: str | CommandSpec) -> tuple[str, str]:
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
        return account, token

    def _verify_identity(self, actual: str) -> None:
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
            raise ConnectorFailure(
                "ADO_ACCESS_DENIED",
                "Azure DevOps authentication or organization access is required",
                auth_required=True,
            )
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
                    url=item.url,
                    reasons=list(item.reasons),
                )
            )
            cutoff = utc_now() - timedelta(days=self.settings.mention_reply_days)
            if updated >= cutoff and int(fields.get("System.CommentCount") or 0) > 0:
                candidates.append((item, record))

        # Bounded, best-effort field history for the latest recent work-item revision.
        by_id = {str(record.get('id')): record for record in records}
        history_targets = [item for item in items if item.updated_at >= utc_now() - timedelta(days=self.settings.mention_reply_days)][:50]
        history_semaphore = asyncio.Semaphore(6)
        async def history(item):
            async with history_semaphore:
                revision = int(by_id.get(item.key, {}).get('rev') or 0)
                if not revision:
                    return None
                cache_key = (item.id, revision)
                if cache_key in self._history_cache:
                    return self._history_cache[cache_key]
                data, _ = await self._json(client, 'GET', f"{self.base_url}/_apis/wit/workItems/{item.key}/updates?$top=2&$skip={max(0, revision - 2)}&api-version={API_VERSION}")
                update = next((value for value in data.get('value', []) if value.get('rev') == revision), None)
                if not update:
                    return None
                changes = ado_changes(update)
                result = (item.id, changes, display_name(update.get('revisedBy')))
                if len(self._history_cache) >= 500:
                    self._history_cache.clear()
                self._history_cache[cache_key] = result
                return result
        try:
            history_results = await asyncio.wait_for(asyncio.gather(*(history(item) for item in history_targets), return_exceptions=True), timeout=5)
        except TimeoutError:
            history_results = []
        for result in history_results:
            if isinstance(result, tuple) and result[1]:
                for activity in activities:
                    if activity.item_id == result[0] and activity.event_type == 'updated':
                        activity.changes = result[1]
                        activity.summary = '; '.join(result[1])
                        activity.detail_source = 'ADO revision history'
                        activity.actor = result[2]
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
        if vote < 0 and "author" in roles:
            reasons.append("requested_changes")
        actionable = ("reviewer" in roles and vote == 0) or ("author" in roles and (vote < 0 or failed_checks > 0))
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
