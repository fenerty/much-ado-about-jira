from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import JiraSettings
from models import Activity, ConnectorHealth, ConnectorResult, WorkItem, utc_now
from safety import assert_jira_read_command, safe_error
from .base import CommandSpec, ConnectorFailure, display_name, local_cli_bridge, parse_datetime, parse_json_output, run_command, unique_strings


class JiraConnector:
    name = "jira"

    def __init__(self, settings: JiraSettings, timeout_seconds: int):
        self.settings = settings
        self.timeout_seconds = timeout_seconds

    async def refresh(self) -> ConnectorResult:
        attempted = utc_now()
        if not self.settings.enabled:
            return self._result("disabled", "Jira connector is disabled", attempted)
        try:
            executable = self._find_cli()
            identity = await asyncio.to_thread(self._authenticate, executable)
            records, query_roles, partial_queries = await self._query_candidates(executable, identity)
            records, hydrate_failures = await self._hydrate_candidates(executable, records)
            partial_queries.extend(hydrate_failures)
            items, activities, mention_capability = await self._normalize(
                executable, records, query_roles, identity
            )
            partial = bool(partial_queries)
            state = "partial" if partial else "ok"
            message = "Jira refreshed successfully"
            if partial:
                message = "Jira refreshed with some unavailable query paths"
            return ConnectorResult(
                connector=self.name,
                work_items=items,
                activities=activities,
                health=ConnectorHealth(
                    connector=self.name,
                    state=state,
                    message=message,
                    last_attempt_at=attempted,
                    last_success_at=utc_now(),
                    coverage={
                        "site": self.settings.site,
                        "work_items": len(items),
                        "activity_projects": list(self.settings.activity_projects),
                        "structured_mentions": mention_capability,
                        "unavailable_queries": partial_queries,
                        "notification_inbox": "unsupported",
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
        except (ValueError, OSError) as exc:
            return self._result("error", "Jira read failed", attempted, exc.__class__.__name__, exc)

    def _result(
        self,
        state: str,
        message: str,
        attempted: datetime,
        code: str | None = None,
        diagnostic: Exception | None = None,
    ) -> ConnectorResult:
        coverage: dict[str, Any] = {"site": self.settings.site}
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

    def _run(self, executable: str | CommandSpec, args: list[str]) -> str:
        assert_jira_read_command(args)
        output = run_command(executable, args, self.timeout_seconds)
        if output.returncode:
            message = (output.stderr or output.stdout).lower()
            auth = any(word in message for word in ("auth", "login", "credential", "unauthorized"))
            raise ConnectorFailure(
                "JIRA_LOGIN_REQUIRED" if auth else "JIRA_COMMAND_FAILED",
                "Atlassian CLI sign-in is required" if auth else "Atlassian CLI read command failed",
                auth_required=auth,
            )
        return output.stdout

    def _authenticate(self, executable: str | CommandSpec) -> dict[str, str]:
        output = self._run(executable, ["jira", "auth", "status"])
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
        identity = {"email": self.settings.expected_account, "account_id": "", "display_name": ""}
        try:
            data = parse_json_output(output)
            if isinstance(data, dict):
                identity["account_id"] = str(data.get("accountId") or data.get("account_id") or "")
                identity["display_name"] = str(data.get("displayName") or identity["display_name"])
        except ValueError:
            pass
        return identity

    async def _query_candidates(
        self, executable: str | CommandSpec, identity: dict[str, str]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]], list[str]]:
        projects = ", ".join(f'"{project}"' for project in self.settings.activity_projects)
        assigned = await asyncio.to_thread(
            self._search,
            executable,
            "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC",
        )
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

        queries = {
            "watching": "watcher = currentUser() AND statusCategory != Done ORDER BY updated DESC",
            "author": "(creator = currentUser() OR reporter = currentUser()) AND statusCategory != Done ORDER BY updated DESC",
        }
        failed: list[str] = []
        if len(assigned) >= self.settings.max_candidates_per_query:
            failed.append("assigned:safety_limit")
        account_id = identity.get("account_id", "")
        if account_id:
            queries["participant_candidate"] = (
                f'project IN ({projects}) AND issue IN updatedBy("{account_id}", '
                f'"-{self.settings.participation_days}d") ORDER BY updated DESC'
            )
            queries["mention_candidate"] = (
                f'project IN ({projects}) AND comment ~ "{account_id}" '
                f'AND updated >= -{self.settings.mention_reply_days}d ORDER BY updated DESC'
            )
        else:
            failed.extend(["participant_candidate:no_account_id", "mention_candidate:no_account_id"])

        async def query(role: str, jql: str) -> tuple[str, list[dict[str, Any]] | None]:
            try:
                found = await asyncio.to_thread(self._search, executable, jql)
            except ConnectorFailure:
                return role, None
            return role, found

        results = await asyncio.gather(*(query(role, jql) for role, jql in queries.items()))
        for role, found in results:
            if found is None:
                failed.append(role)
                continue
            if len(found) >= self.settings.max_candidates_per_query:
                failed.append(f"{role}:safety_limit")
            for row in found[: self.settings.max_candidates_per_query]:
                key = self._key(row)
                if not key:
                    continue
                records[key] = row
                roles.setdefault(key, set()).add(role)
        return records, roles, failed

    def _search(self, executable: str | CommandSpec, jql: str) -> list[dict[str, Any]]:
        fields = "key,summary,status,priority,assignee,creator,reporter"
        output = self._run(
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
                "--paginate",
            ],
        )
        return self._records(parse_json_output(output))

    async def _hydrate_candidates(
        self, executable: str | CommandSpec, records: dict[str, dict[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        semaphore = asyncio.Semaphore(8)

        async def hydrate(key: str) -> tuple[str, dict[str, Any] | None]:
            async with semaphore:
                try:
                    row = await asyncio.to_thread(self._view, executable, key)
                except ConnectorFailure:
                    return key, None
                return key, row

        hydrated: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        for key, row in await asyncio.gather(*(hydrate(key) for key in records)):
            if row is None:
                failures.append(f"view:{key}")
            else:
                hydrated[key] = row
        return hydrated, failures

    def _view(self, executable: str | CommandSpec, key: str) -> dict[str, Any]:
        output = self._run(
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
            if "assigned" in roles and isinstance(assignee, dict) and not identity.get("account_id"):
                identity["account_id"] = str(assignee.get("accountId") or "")
            reasons = [role for role in ("assigned", "watching", "author") if role in roles]
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
                comments = await asyncio.to_thread(self._comment_list, executable, key)
            comment_reasons, comment_activity, structured = self._comment_signals(
                key, title, comments, identity
            )
            structured_mentions_seen = structured_mentions_seen or structured
            reasons.extend(comment_reasons)
            if "participant_candidate" in roles and "participant" not in reasons:
                roles.discard("participant_candidate")
            if not reasons:
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
                metadata={"status_category_source": category_name or "derived"},
            )
            if category != "done" or bool({"mentioned", "replied", "participant"} & set(reasons)):
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

    def _comment_list(self, executable: str | CommandSpec, key: str) -> list[dict[str, Any]]:
        output = self._run(
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
        account_id = str(author.get("accountId") or "").lower()
        email = str(author.get("emailAddress") or "").lower()
        name = str(author.get("displayName") or "").lower()
        return bool(
            (identity.get("account_id") and account_id == identity["account_id"].lower())
            or (identity.get("email") and email == identity["email"].lower())
            or (identity.get("display_name") and name == identity["display_name"].lower())
        )

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
        combined = f"{status} {category}".lower()
        if any(word in combined for word in ("done", "closed", "resolved", "complete")):
            return "done"
        if "block" in combined or "pending" in combined:
            return "blocked"
        if any(word in combined for word in ("progress", "implement", "develop", "review")):
            return "in_progress"
        if any(word in combined for word in ("to do", "open", "ready", "new", "backlog")):
            return "todo"
        return "other"
