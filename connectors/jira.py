from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from config import JiraSettings, source_scope
from models import Activity, ConnectorHealth, ConnectorResult, WorkItem, utc_now
from safety import assert_jira_read_command, safe_error
from .base import CommandSpec, ConnectorFailure, async_run_command, display_name, local_cli_bridge, parse_datetime, parse_json_output, unique_strings
from .jira_history import HISTORY_ROLES, JiraHistory


class JiraConnector:
    name = "jira"

    def __init__(self, settings: JiraSettings, timeout_seconds: int):
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self._saved_state: dict = {}
        self._deadline: float | None = None
        self._command_slots = asyncio.Semaphore(4)
        self._fresh_roles: dict[str, set[str]] = {}
        self._verified_history_roles: dict[str, set[str]] = {}
        self._enrichment_failures: list[str] = []
        self.state_loader = None
        self.scope_verified: Callable[[str], None] | None = None
        self.verified_cache_scope: str | None = None

    def load_state(self, state: dict) -> None:
        self._saved_state = state
        if hasattr(self, "_history"):
            del self._history

    def checkpoint(self) -> dict:
        return self._history.checkpoint() if hasattr(self, "_history") else {}

    def _ensure_history(self, identity: dict[str, str]) -> None:
        scope = hashlib.sha256(json.dumps({
            "site": identity.get("site") or self.settings.site,
            "email": identity.get("email") or self.settings.expected_account,
            "projects": self.settings.activity_projects,
            "participation_days": self.settings.participation_days,
            "mention_reply_days": self.settings.mention_reply_days,
        }, sort_keys=True).encode()).hexdigest()
        if not hasattr(self, "_history") or self._history.scope != scope:
            self._history = JiraHistory(scope, self._saved_state)

    async def refresh(self) -> ConnectorResult:
        self.verified_cache_scope = None
        attempted = utc_now()
        if not self.settings.enabled:
            return self._result("disabled", "Jira connector is disabled", attempted)
        self._deadline = time.monotonic() + self.settings.refresh_timeout_seconds
        self._command_slots = asyncio.Semaphore(4)
        self._enrichment_failures = []
        try:
            executable = self._find_cli()
            identity = await self._authenticate(executable)
            self.verified_cache_scope = source_scope(self.name, identity["site"], identity["email"])
            if self.scope_verified is not None:
                self.scope_verified(self.verified_cache_scope)
            if self.state_loader is not None:
                self.load_state(self.state_loader(self.verified_cache_scope))
            candidates, query_roles, partial_queries = await self._query_candidates(executable, identity, include_history=False)
            records, hydrate_failures = await self._hydrate_candidates(executable, candidates, include_history=False)
            partial_queries.extend(hydrate_failures)
            # Commit active reads to this result before spending any history budget.
            await self._discover_history(executable, candidates, query_roles, partial_queries)
            history_candidates = {key: {"key": key, "fields": {"status": {"name": "Done"}}} for key in self._history.records}
            historical, history_failures = await self._hydrate_candidates(executable, history_candidates, verified=set(records))
            records.update(historical)
            partial_queries.extend(history_failures)
            items, activities, mention_capability = await self._normalize(
                executable, records, query_roles, identity
            )
            partial_queries.extend(self._enrichment_failures)
            partial = bool(partial_queries)
            state = "partial" if partial else "ok"
            message = "Jira refreshed successfully"
            if partial:
                message = "Jira refreshed with some unavailable query paths"
                if set(partial_queries) == {"closed_history:rotating_batch"}:
                    message = "Jira refreshed; older history discovery and details advance in saved batches"
            return ConnectorResult(
                connector=self.name,
                cache_scope=self.verified_cache_scope,
                work_items=items,
                activities=activities,
                health=ConnectorHealth(
                    connector=self.name,
                    state=state,
                    message=message,
                    last_attempt_at=attempted,
                    last_success_at=utc_now(),
                    coverage={
                        "completed_history": {**getattr(self, "_history_progress", {}), "last_sync_seconds": (utc_now() - attempted).total_seconds()},
                        "history_discovery": self._discovery_progress,
                        "site": self.settings.site,
                        "work_items": len(items),
                        "activity_projects": list(self.settings.activity_projects),
                        "comment_discovery_enabled": bool(self.settings.activity_projects),
                        "structured_mentions": mention_capability,
                        "unavailable_queries": partial_queries,
                        "notification_inbox": "unsupported",
                        "previous_assignment_query": not any(query.startswith("previously_assigned:") and ":completed:" not in query for query in partial_queries),
                        "historical_coverage": "Partial: comment discovery is bounded by configured projects and lookback windows",
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
        except (ValueError, OSError) as exc:
            return self._result(
                "error", "Jira read failed", attempted, exc.__class__.__name__, exc,
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
        coverage: dict[str, Any] = {"site": self.settings.site}
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
        candidates = [configured, shutil.which("acli")]
        project_local = Path(__file__).resolve().parents[1] / ".tools" / "acli.exe"
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        candidates.extend(
            [
                str(project_local),
                str(local_app_data / "MuchADOAboutJira" / "tools" / "acli.exe"),
                str(local_app_data / "Atlassian" / "acli" / "acli.exe"),
                str(local_app_data / "Programs" / "acli" / "acli.exe"),
            ]
        )
        executable = next((value for value in candidates if value and Path(value).exists()), None)
        if executable:
            return executable
        return local_cli_bridge("acli")

    async def _run(self, executable: str | CommandSpec, args: list[str]) -> str:
        assert_jira_read_command(args)
        async with self._command_slots:
            remaining = self.timeout_seconds if self._deadline is None else self._deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectorFailure("JIRA_TIMEOUT", "Jira refresh budget exhausted")
            try:
                output = await async_run_command(executable, args, min(self.timeout_seconds, remaining))
            except subprocess.TimeoutExpired:
                raise ConnectorFailure("JIRA_TIMEOUT", "Jira read command timed out") from None
        if output.returncode:
            message = (output.stderr or output.stdout).lower()
            auth = any(word in message for word in ("auth", "login", "credential", "unauthorized"))
            raise ConnectorFailure(
                "JIRA_LOGIN_REQUIRED" if auth else "JIRA_COMMAND_FAILED",
                "Atlassian CLI sign-in is required" if auth else "Atlassian CLI read command failed",
                auth_required=auth,
            )
        return output.stdout

    async def _authenticate(self, executable: str | CommandSpec) -> dict[str, str]:
        output = await self._run(executable, ["jira", "auth", "status"])
        expected = self.settings.expected_account.strip().lower()
        if expected and expected not in output.lower():
            try:
                data = parse_json_output(output)
                serialized = json.dumps(data).lower()
            except ValueError:
                serialized = output.lower()
            if expected not in serialized:
                raise ConnectorFailure(
                    "IDENTITY_MISMATCH",
                    "Atlassian CLI identity does not match the configured corporate account",
                    auth_required=True,
                )
        identity = {"email": "", "account_id": "", "display_name": "", "site": ""}
        try:
            data = parse_json_output(output)
            if isinstance(data, dict):
                identity["account_id"] = str(data.get("accountId") or data.get("account_id") or "")
                identity["display_name"] = str(data.get("displayName") or identity["display_name"])
                identity["email"] = str(data.get("email") or data.get("emailAddress") or identity["email"])
                identity["site"] = str(data.get("site") or "")
        except ValueError:
            pass
        email = re.search(r"(?im)^[ \t]*Email:[ \t]*(\S+)", output)
        site = re.search(r"(?im)^[ \t]*Site:[ \t]*(\S+)", output)
        if email:
            identity["email"] = email.group(1).lower()
        if site:
            identity["site"] = site.group(1)
        configured_site = urlparse(self.settings.site).hostname or self.settings.site
        actual_site = urlparse("https://" + identity["site"].removeprefix("https://").rstrip("/")).hostname
        identity["email"] = identity["email"].strip().lower()
        if not identity["email"] or not actual_site:
            raise ConnectorFailure("IDENTITY_UNAVAILABLE", "Atlassian CLI did not expose an account and site", auth_required=True)
        if expected and identity["email"] != expected:
            raise ConnectorFailure(
                "IDENTITY_MISMATCH",
                "Atlassian CLI identity does not match the configured corporate account",
                auth_required=True,
            )
        if configured_site and actual_site.lower() != configured_site.lower():
            raise ConnectorFailure("SITE_MISMATCH", "Atlassian CLI site does not match the configured Jira site", auth_required=True)
        identity["site"] = actual_site.lower()
        return identity

    async def _query_candidates(
        self, executable: str | CommandSpec, identity: dict[str, str], *, include_history: bool = True
    ) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]], list[str]]:
        projects = ", ".join(f'"{project}"' for project in self.settings.activity_projects)
        self._ensure_history(identity)
        assigned = await self._search(executable, "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC")
        records: dict[str, dict[str, Any]] = {}
        roles: dict[str, set[str]] = {}
        for row in assigned[: self.settings.max_candidates_per_query]:
            key = self._key(row)
            if not key:
                continue
            records[key] = row
            roles.setdefault(key, set()).add("assigned")
            assignee = (row.get("fields") or row).get("assignee") or {}
            if isinstance(assignee, dict) and not identity.get("account_id"):
                identity["account_id"] = str(assignee.get("accountId") or "")

        relationships = {
            "assigned": "assignee = currentUser()",
            "previously_assigned": "assignee WAS currentUser() AND (assignee != currentUser() OR assignee IS EMPTY) ORDER BY updated DESC",
            "watching": "watcher = currentUser() ORDER BY updated DESC",
            "author": "(creator = currentUser() OR reporter = currentUser()) ORDER BY updated DESC",
        }
        relationships = {role: jql.removesuffix(" ORDER BY updated DESC") for role, jql in relationships.items()}
        queries = {role: f"({jql}) AND statusCategory != Done ORDER BY updated DESC" for role, jql in relationships.items() if role != "assigned"}
        failed: list[str] = []
        if len(assigned) >= self.settings.max_candidates_per_query:
            failed.append("assigned:safety_limit")
        account_id = identity.get("account_id", "")
        if account_id and self.settings.activity_projects:
            queries["participant_candidate"] = (
                f'project IN ({projects}) AND issue IN updatedBy("{account_id}", '
                f'"-{self.settings.participation_days}d") ORDER BY updated DESC'
            )
            queries["mention_candidate"] = (
                f'project IN ({projects}) AND comment ~ "{account_id}" '
                f'AND updated >= -{self.settings.mention_reply_days}d ORDER BY updated DESC'
            )
        elif self.settings.activity_projects:
            failed.extend(["participant_candidate:no_account_id", "mention_candidate:no_account_id"])

        async def query(role: str, jql: str) -> tuple[str, list[dict[str, Any]] | None]:
            try:
                found = await self._search(executable, jql)
            except (ConnectorFailure, ValueError, OSError) as exc:
                failed.append(f"{role}:{getattr(exc, 'code', type(exc).__name__)}")
                return role, None
            return role, found

        results = await asyncio.gather(*(query(role, jql) for role, jql in queries.items()))
        for role, found in results:
            if found is None:
                continue
            if len(found) >= self.settings.max_candidates_per_query:
                failed.append(f"{role}:safety_limit")
            for row in found[: self.settings.max_candidates_per_query]:
                key = self._key(row)
                if not key:
                    continue
                records[key] = row
                roles.setdefault(key, set()).add(role)
        self._fresh_roles = {key: set(value) for key, value in roles.items()}
        self._verified_history_roles = {}
        if include_history:
            await self._discover_history(executable, records, roles, failed)
        return records, roles, failed

    async def _discover_history(self, executable, records, roles, failed) -> None:
        relationships = {
            "assigned": "assignee = currentUser()",
            "previously_assigned": "assignee WAS currentUser() AND (assignee != currentUser() OR assignee IS EMPTY)",
            "watching": "watcher = currentUser()",
            "author": "creator = currentUser() OR reporter = currentUser()",
        }

        # Discovery is a single limited page after active work, with an all-time
        # keyset cursor per relationship. A failed role is retried on rotation.
        role = HISTORY_ROLES[self._history.next_role]
        self._history.next_role = (self._history.next_role + 1) % len(HISTORY_ROLES)
        cursor = self._history.cursors[role]
        jql = f"({relationships[role]}) AND statusCategory = Done"
        if cursor:
            jql += f" AND key > {json.dumps(cursor)}"
        jql += " ORDER BY key ASC"
        page_size = min(self.settings.history_batch_size, self.settings.max_candidates_per_query)
        try:
            page = await asyncio.wait_for(self._search(executable, jql, limit=page_size), timeout=self.settings.history_timeout_seconds)
            self._history.accept_page(role, [self._key(row) for row in page], len(page) >= page_size)
            self._verified_history_roles = {self._key(row): {role} for row in page}
        except (TimeoutError, ConnectorFailure, ValueError, OSError) as exc:
            failed.append(f"{role}:completed:{getattr(exc, 'code', type(exc).__name__)}")
        self._discovery_progress = {
            "batch_size": page_size, "last_role": role,
            "roles_with_completed_pass": sum(count > 0 for count in self._history.passes.values()),
            "total_roles": len(HISTORY_ROLES), "passes": dict(self._history.passes),
            "remaining_source_items": "unknown", "all_time_queries": True,
        }
        for key, historical_roles in self._history.records.items():
            if key not in records:
                records[key] = {"key": key, "fields": {"status": {"name": "Done"}}, "history_only": True}
            roles.setdefault(key, set()).update(historical_roles)
        if not all(self._history.passes.values()):
            failed.append("closed_history:rotating_batch")

    async def _search(self, executable: str | CommandSpec, jql: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        fields = "key,summary,status,priority,assignee,creator,reporter"
        output = await self._run(
            executable,
            [
                "jira",
                "workitem",
                "search",
                "--jql",
                jql,
                "--fields",
                fields,
                "--json",
                "--limit",
                str(limit if limit is not None else self.settings.max_candidates_per_query),
            ],
        )
        data = parse_json_output(output)
        if isinstance(data, list) and any(not isinstance(row, dict) or not self._key(row) for row in data):
            raise ValueError("Jira search returned invalid work items")
        return self._records(data)

    async def _hydrate_candidates(
        self, executable: str | CommandSpec, records: dict[str, dict[str, Any]], *, include_history: bool = True, verified: set[str] | None = None
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        semaphore = asyncio.Semaphore(8)

        async def hydrate(key: str) -> tuple[str, dict[str, Any] | None]:
            async with semaphore:
                try:
                    row = await self._view(executable, key)
                except (ConnectorFailure, ValueError, OSError):
                    return key, None
                return key, row

        # ACLI search cannot return timestamps. Rotate a bounded completed-item
        # batch so historical detail cannot starve active-work refreshes.
        closed = []
        candidates = []
        for key, row in records.items():
            fields = row.get('fields') or row
            status = fields.get('status') or {}
            name = str(status.get('name') or '') if isinstance(status, dict) else str(status)
            category = str((status.get('statusCategory') or {}).get('name') or '') if isinstance(status, dict) else ''
            if self._status_category(name, category) == 'done' and (include_history or key not in self._fresh_roles):
                closed.append(key)
            else:
                candidates.append(key)
        if not hasattr(self, "_history"):
            self._ensure_history({})
        for key in closed:
            self._history.records.setdefault(key, {"previously_assigned"})
        hydrated: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        # Active hydration completes before historical work starts.
        for key, row in await asyncio.gather(*(hydrate(key) for key in candidates)):
            if row is None:
                failures.append(f"view:{key}")
            else:
                hydrated[key] = row
        if not include_history:
            return hydrated, failures
        batch = self._history.hydration_batch(16, verified=set(hydrated) | (verified or set()))
        # Preserve each completed view when the history phase reaches its deadline.
        history_tasks = [asyncio.create_task(hydrate(key)) for key in batch]
        try:
            done, _ = await asyncio.wait(history_tasks, timeout=self.settings.history_timeout_seconds) if history_tasks else (set(), set())
        finally:
            for task in history_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*history_tasks, return_exceptions=True)
        completed = [task.result() for task in done if not task.cancelled() and task.exception() is None]
        for key, row in completed:
            if row is None:
                failures.append(f"view:{key}")
            else:
                hydrated[key] = row
        failures.extend(f"view:{key}" for key in batch if key not in hydrated and f"view:{key}" not in failures)
        self._history.checked.update(key for key in batch if key in hydrated)
        remaining = len(self._history.records) - len(self._history.checked)
        if remaining:
            failures.append("closed_history:rotating_batch")
        self._history_progress = {'total': len(self._history.records), 'checked': len(self._history.checked), 'remaining': remaining, 'batches_remaining': (remaining + 15) // 16, 'failed': sum(key not in hydrated for key in batch), 'scope': 'discovered history only; discovery may still be in progress'}
        return hydrated, failures

    async def _view(self, executable: str | CommandSpec, key: str) -> dict[str, Any]:
        output = await self._run(
            executable,
            [
                "jira",
                "workitem",
                "view",
                key,
                "--fields",
                "key,summary,status,priority,assignee,creator,reporter,created,updated,comment",
                "--json",
            ],
        )
        rows = self._records(parse_json_output(output))
        if not rows:
            raise ConnectorFailure("JIRA_EMPTY_VIEW", "Atlassian CLI returned no work item")
        return rows[0]

    @staticmethod
    def _records(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("issues", "workItems", "workitems", "comments", "results", "values", "value", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
            if isinstance(value, dict):
                nested = JiraConnector._records(value)
                if nested:
                    return nested
        if data.get("key"):
            return [data]
        return []

    @staticmethod
    def _key(row: dict[str, Any]) -> str:
        fields = row.get("fields") or {}
        return str(row.get("key") or fields.get("key") or "")

    async def _normalize(
        self,
        executable: str | CommandSpec,
        records: dict[str, dict[str, Any]],
        query_roles: dict[str, set[str]],
        identity: dict[str, str],
    ) -> tuple[list[WorkItem], list[Activity], bool]:
        items: list[WorkItem] = []
        activities: list[Activity] = []
        structured_mentions_seen = False
        for key, row in records.items():
            fields = row.get("fields") or row
            status_data = fields.get("status") or {}
            status = display_name(status_data) or str(status_data or "Unknown")
            category_name = str((status_data.get("statusCategory") or {}).get("name") or "") if isinstance(status_data, dict) else ""
            category = self._status_category(status, category_name)
            roles = set(query_roles.get(key, set()))
            assignee = fields.get("assignee") or {}
            historical_roles = self._history.records.get(key, set()) if hasattr(self, "_history") else set()
            if historical_roles:
                # Saved discovery evidence is not a current relationship. In
                # particular, a reopened/reassigned ticket must not be assigned.
                roles = set(self._fresh_roles.get(key, set())) | self._verified_history_roles.get(key, set())
                if "assigned" in historical_roles or "previously_assigned" in historical_roles:
                    roles.discard("assigned")
                    roles.add("assigned" if self._is_self(assignee, identity) else "previously_assigned")
                if "author" in historical_roles and any(self._is_self(fields.get(field) or {}, identity) for field in ("creator", "reporter")):
                    roles.add("author")
            reasons = [role for role in ("assigned", "watching", "author", "previously_assigned") if role in roles]
            if "author" in roles:
                reasons.append("waiting")
            title = str(fields.get("summary") or key)
            updated = parse_datetime(fields.get("updated")) or utc_now()
            comment_value = fields.get("comment")
            comments = self._comments(comment_value)
            comment_total = self._comment_total(comment_value)
            if (not comments or comment_total > len(comments)) and (
                "participant_candidate" in roles or "mention_candidate" in roles
            ):
                try:
                    comments = await self._comment_list(executable, key)
                except (ConnectorFailure, ValueError, OSError) as exc:
                    self._enrichment_failures.append(f"comments:{key}:{getattr(exc, 'code', type(exc).__name__)}")
            comment_reasons, comment_activity, structured = self._comment_signals(
                key, title, comments, identity
            )
            structured_mentions_seen = structured_mentions_seen or structured
            reasons.extend(comment_reasons)
            if "participant_candidate" in roles and "participant" not in reasons:
                roles.discard("participant_candidate")
            if not reasons and not historical_roles:
                continue
            project = key.split("-", 1)[0]
            item = WorkItem(
                id=f"jira:issue:{key}",
                source="jira",
                source_type="issue",
                project=project,
                key=key,
                title=title,
                url=f"{self.settings.site.rstrip('/')}/browse/{key}",
                status=status,
                status_category=category,
                priority=display_name(fields.get("priority")),
                assigned_to=display_name(fields.get("assignee")),
                author=display_name(fields.get("creator") or fields.get("reporter")),
                updated_at=updated,
                created_at=parse_datetime(fields.get("created")),
                actionable=(category == "blocked" or bool({"mentioned", "replied"} & set(reasons))),
                reasons=unique_strings(reasons),
                metadata={"status_category_source": category_name or "derived", **({"tracked_relationships": sorted(historical_roles)} if historical_roles else {})},
            )
            items.append(item)
            activities.append(
                Activity(
                    id=f"jira:issue:{key}:updated:{updated.isoformat()}",
                    source="jira",
                    event_type="updated",
                    item_id=item.id,
                    item_key=key,
                    item_title=title,
                    timestamp=updated,
                    summary="Issue updated",
                    url=item.url,
                    reasons=list(item.reasons),
                )
            )
            activities.extend(comment_activity)
        return items, activities, structured_mentions_seen

    async def _comment_list(self, executable: str | CommandSpec, key: str) -> list[dict[str, Any]]:
        output = await self._run(
            executable,
            ["jira", "workitem", "comment", "list", "--key", key, "--json", "--paginate"],
        )
        return self._records(parse_json_output(output))

    @staticmethod
    def _comments(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            for key in ("comments", "values", "value"):
                if isinstance(value.get(key), list):
                    return [row for row in value[key] if isinstance(row, dict)]
        return []

    @staticmethod
    def _comment_total(value: Any) -> int:
        if isinstance(value, dict):
            try:
                return int(value.get("total") or 0)
            except (TypeError, ValueError):
                return 0
        return len(value) if isinstance(value, list) else 0

    def _comment_signals(
        self,
        key: str,
        title: str,
        comments: list[dict[str, Any]],
        identity: dict[str, str],
    ) -> tuple[list[str], list[Activity], bool]:
        comments.sort(key=lambda row: str(row.get("created") or row.get("updated") or ""))
        if not identity.get("account_id"):
            for comment in comments:
                author = comment.get("author") or {}
                if self._is_self(author, identity) and author.get("accountId"):
                    identity["account_id"] = str(author["accountId"])
                    break
        reasons: list[str] = []
        activity: list[Activity] = []
        last_self: datetime | None = None
        structured_seen = False
        now = utc_now()
        participation_cutoff = now - timedelta(days=self.settings.participation_days)
        mention_cutoff = now - timedelta(days=self.settings.mention_reply_days)
        for comment in comments:
            author = comment.get("author") or {}
            is_self = self._is_self(author, identity)
            created = parse_datetime(comment.get("created") or comment.get("updated")) or utc_now()
            if is_self and created >= participation_cutoff:
                reasons.append("participant")
                last_self = created
            text, mention_ids = self._adf_text_and_mentions(comment.get("body"))
            structured_seen = structured_seen or bool(mention_ids)
            account_id = identity.get("account_id", "").lower()
            recent = created >= mention_cutoff
            mentioned = bool(
                recent and account_id and account_id in {value.lower() for value in mention_ids}
            )
            replied = bool(recent and last_self and not is_self and created > last_self)
            if mentioned:
                reasons.append("mentioned")
            if replied:
                reasons.append("replied")
            if mentioned or replied:
                comment_id = str(comment.get("id") or created.isoformat())
                activity.append(
                    Activity(
                        id=f"jira:issue:{key}:comment:{comment_id}",
                        source="jira",
                        event_type="mention" if mentioned else "reply",
                        actor=display_name(author),
                        item_id=f"jira:issue:{key}",
                        item_key=key,
                        item_title=title,
                        timestamp=created,
                        summary=(" ".join(text.split())[:180] or "New comment"),
                        url=f"{self.settings.site.rstrip('/')}/browse/{key}?focusedCommentId={comment_id}",
                        reasons=["mentioned"] if mentioned else ["replied"],
                    )
                )
        return unique_strings(reasons), activity, structured_seen

    @staticmethod
    def _is_self(author: dict[str, Any], identity: dict[str, str]) -> bool:
        account_id = str(author.get("accountId") or "").strip().lower()
        self_account_id = str(identity.get("account_id") or "").strip().lower()
        if account_id and self_account_id:
            return account_id == self_account_id
        email = str(author.get("emailAddress") or "").strip().lower()
        self_email = str(identity.get("email") or "").strip().lower()
        if email and self_email:
            return email == self_email
        name = str(author.get("displayName") or "").strip().lower()
        self_name = str(identity.get("display_name") or "").strip().lower()
        return bool(name and self_name and name == self_name)

    @staticmethod
    def _adf_text_and_mentions(value: Any) -> tuple[str, list[str]]:
        if isinstance(value, str):
            return value, []
        text: list[str] = []
        mentions: list[str] = []

        def visit(node: Any) -> None:
            if isinstance(node, list):
                for child in node:
                    visit(child)
                return
            if not isinstance(node, dict):
                return
            node_type = node.get("type")
            attrs = node.get("attrs") or {}
            if node_type == "text" and node.get("text"):
                text.append(str(node["text"]))
            elif node_type == "mention":
                if attrs.get("id"):
                    mentions.append(str(attrs["id"]))
                if attrs.get("text"):
                    text.append(str(attrs["text"]))
            elif node_type in {"paragraph", "heading", "listItem"} and text:
                text.append(" ")
            visit(node.get("content"))

        visit(value)
        return "".join(text), mentions

    @staticmethod
    def _status_category(status: str, category: str) -> str:
        status_name = " ".join(status.lower().split())
        category_name = " ".join(category.lower().split())
        source_category = {"done": "done", "in progress": "in_progress", "to do": "todo"}.get(category_name)
        if source_category == "done":
            return "done"
        if re.search(r"\b(?:blocked|pending)\b", status_name):
            return "blocked"
        if source_category:
            return source_category
        if status_name in {"done", "closed", "resolved", "complete", "completed"}:
            return "done"
        if status_name in {"in progress", "implementing", "implementation", "developing", "development", "in review", "review"}:
            return "in_progress"
        if status_name in {"to do", "open", "ready", "new", "backlog"}:
            return "todo"
        return "other"
