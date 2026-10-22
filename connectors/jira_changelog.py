"""Read-only Jira changelogs and minimal, resumable account-scoped progress.

API tokens are supplied only by the process environment. Neither raw changelogs
nor credentials belong in the checkpoint or in diagnostic messages.
"""
from __future__ import annotations

import asyncio
import copy
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode, urlparse

import httpx

from activity_details import MAX_CHANGES, ado_actor, value_text
from models import Activity, utc_now
from safety import safe_error
from .base import parse_datetime


KEY_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*-[0-9]+\Z")
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
VALUE_FIELDS = {
    "status", "priority", "summary", "resolution", "issuetype", "labels",
    "components", "fixversions", "versions", "duedate",
}
IDENTITY_FIELDS = {"assignee", "reporter", "creator"}
FIELD_LABELS = {"summary": "Title", "issuetype": "Issue type", "duedate": "Due date",
                "fixversions": "Fix versions", "versions": "Affects versions"}


def valid_timestamp(value):
    timestamp = parse_datetime(value)
    if timestamp is None or timestamp.tzinfo is None or timestamp > utc_now() + timedelta(days=1):
        return None
    return timestamp


def jira_changes(items: list[dict]) -> list[str]:
    """Only known scalar fields expose values; bodies/custom fields get a label.

    Jira's changelog schema does not identify custom rich-text field types, so
    copying arbitrary fromString/toString values could retain entire bodies.
    """
    changes = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("field"), str):
            raise ValueError("Invalid Jira changelog field")
        name = item["field"].strip()
        if not name:
            raise ValueError("Invalid Jira changelog field")
        reference = str(item.get("fieldId") or name).lower()
        label = FIELD_LABELS.get(reference) or safe_error(name, limit=80)
        label = label[:1].upper() + label[1:]
        if reference in IDENTITY_FIELDS:
            before = ado_actor(item.get("fromString")) if item.get("fromString") else (
                "(identity)" if item.get("from") else "None")
            after = ado_actor(item.get("toString")) if item.get("toString") else (
                "(identity)" if item.get("to") else "None")
            changes.append(f"{label}: {before or '(identity)'} → {after or '(identity)'}")
        elif reference in VALUE_FIELDS and all(
            item.get(key) is None or isinstance(item.get(key), (str, int, float, bool))
            for key in ("fromString", "toString")
        ) and ("fromString" in item or "toString" in item):
            changes.append(f"{label}: {value_text(item.get('fromString'))} → {value_text(item.get('toString'))}")
        else:
            changes.append(f"{label} changed")
    if len(changes) > MAX_CHANGES:
        changes = changes[:MAX_CHANGES - 1] + [f"{len(changes) - MAX_CHANGES + 1} other fields changed"]
    return changes


def parse_changelog_page(data: dict, offset: int, page_size: int) -> tuple[list[dict], bool]:
    if not isinstance(data, dict) or not isinstance(data.get("values"), list):
        raise ValueError("Invalid Jira changelog page")
    rows = data["values"]
    if (len(rows) > page_size or type(data.get("startAt", offset)) is not int
            or data.get("startAt", offset) != offset):
        raise ValueError("Invalid Jira changelog offset")
    total = data.get("total")
    if total is not None and (type(total) is not int or total < offset + len(rows)):
        raise ValueError("Invalid Jira changelog total")
    last = data.get("isLast")
    if type(last) is not bool:
        if total is None:
            raise ValueError("Jira changelog page lacks completion information")
        last = offset + len(rows) >= total
    elif total is not None and last != (offset + len(rows) >= total):
        raise ValueError("Inconsistent Jira changelog completion information")
    if not rows and not last:
        raise ValueError("Jira changelog page did not advance")
    identifiers = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid Jira changelog row")
        identifier = row.get("id")
        if not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier) or identifier in identifiers:
            raise ValueError("Jira changelog lacks unique stable IDs")
        identifiers.add(identifier)
        if valid_timestamp(row.get("created")) is None or not isinstance(row.get("items"), list):
            raise ValueError("Invalid Jira changelog timestamp or fields")
        jira_changes(row["items"])
    return rows, last


def changelog_activity(target: dict, row: dict) -> Activity:
    timestamp = valid_timestamp(row.get("created"))
    if timestamp is None:
        raise ValueError("Invalid Jira changelog timestamp")
    changes = jira_changes(row["items"])
    identifier = row["id"]
    return Activity(
        id=f"jira:issue:{target['key']}:change:{identifier}", source="jira", event_type="updated",
        item_id=target["id"], item_key=target["key"], item_title=target["title"],
        timestamp=timestamp, actor=ado_actor(row.get("author")),
        summary="; ".join(changes) if changes else "Administrative update; no user-facing field changes",
        changes=changes, detail_source="Jira change history",
        detail_status="captured" if changes else "administrative", source_update_id=identifier,
        legacy_activity_id=f"jira:issue:{target['key']}:updated:{timestamp.isoformat()}",
        url=target["url"], reasons=target["reasons"],
    )


class ChangelogBudgetExhausted(Exception):
    pass


class ChangelogReadError(Exception):
    def __init__(self, code: str, retry_at: str = ""):
        super().__init__(code)
        self.code = code
        self.retry_at = retry_at


def jira_origin(site: str, verified_site: str) -> str:
    parsed = urlparse(site)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or parsed.port not in {None, 443} or parsed.hostname.lower() != verified_site.lower()):
        raise ValueError("Invalid Jira REST origin")
    return f"https://{parsed.hostname.lower()}"


class JiraChangelogReader:
    def __init__(self, client: httpx.AsyncClient, origin: str, timeout_seconds: float):
        self.client = client
        self.origin = origin
        self.timeout_seconds = timeout_seconds

    async def get_json(self, path: str, deadline: float) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChangelogBudgetExhausted()
        phase_limited = remaining <= self.timeout_seconds
        timeout = min(self.timeout_seconds, remaining)
        try:
            response = await asyncio.wait_for(self.client.get(self.origin + path, timeout=timeout), timeout=timeout)
        except (TimeoutError, httpx.TimeoutException):
            if phase_limited or time.monotonic() >= deadline:
                raise ChangelogBudgetExhausted() from None
            raise ChangelogReadError("JIRA_CHANGELOG_TIMEOUT") from None
        except httpx.HTTPError:
            raise ChangelogReadError("JIRA_CHANGELOG_NETWORK_ERROR") from None
        if response.status_code != 200:
            retry_at = ""
            if response.status_code == 429:
                value = response.headers.get("Retry-After", "")
                try:
                    seconds = max(1, min(86400, int(value)))
                    retry_at = (utc_now() + timedelta(seconds=seconds)).isoformat()
                except ValueError:
                    try:
                        when = parsedate_to_datetime(value)
                        if when.tzinfo is not None:
                            retry_at = min(utc_now() + timedelta(days=1), max(utc_now(), when)).isoformat()
                    except (ValueError, TypeError, OverflowError):
                        pass
            raise ChangelogReadError(f"JIRA_CHANGELOG_HTTP_{response.status_code}", retry_at)
        try:
            data = response.json()
        except ValueError:
            raise ChangelogReadError("JIRA_CHANGELOG_INVALID_JSON") from None
        if not isinstance(data, dict):
            raise ChangelogReadError("JIRA_CHANGELOG_INVALID_JSON")
        return data

    async def identity(self, deadline: float) -> dict:
        return await self.get_json("/rest/api/3/myself", deadline)

    async def page(self, key: str, offset: int, page_size: int, deadline: float) -> dict:
        if not KEY_PATTERN.fullmatch(key) or type(offset) is not int or offset < 0:
            raise ValueError("Invalid Jira changelog target")
        # Never follow response nextPage/self URLs or forward auth to another host.
        path = f"/rest/api/3/issue/{quote(key, safe='')}/changelog?" + urlencode(
            {"startAt": offset, "maxResults": page_size})
        return await self.get_json(path, deadline)


class JiraChangelogHistory:
    """Append-position checkpoints with fair rotation and stable event IDs."""
    def __init__(self, scope: str, saved: dict | None = None):
        saved = saved if isinstance(saved, dict) else {}
        if saved.get("version") != 1 or saved.get("scope") != scope:
            saved = {}
        self.scope = scope
        retention = saved.get("retention_days")
        self.retention_days = retention if type(retention) is int and retention > 0 else None
        self.cursor = saved.get("cursor") if isinstance(saved.get("cursor"), str) else ""
        self.retry_at = saved.get("retry_at") if valid_timestamp(saved.get("retry_at")) else ""
        self.records = {}
        records = saved.get("records")
        for key, record in records.items() if isinstance(records, dict) else []:
            if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key) or not isinstance(record, dict):
                continue
            offset = record.get("offset", 0)
            if type(offset) is not int or offset < 0:
                continue
            seen = record.get("seen")
            self.records[key] = {
                "offset": offset,
                "status": record.get("status") if record.get("status") in {"pending", "captured", "failed"} else "pending",
                "seen": {identifier: value for identifier, value in seen.items()
                         if isinstance(identifier, str) and ID_PATTERN.fullmatch(identifier) and valid_timestamp(value)}
                        if isinstance(seen, dict) else {},
                "observed_at": record.get("observed_at") if valid_timestamp(record.get("observed_at")) else "",
                "error_code": str(record.get("error_code") or "")[:80],
                "retry_at": record.get("retry_at") if valid_timestamp(record.get("retry_at")) else "",
            }

    def prepare(self, keys: set[str], cutoff: datetime, retention_days: int) -> None:
        self.records = {key: record for key, record in self.records.items() if key in keys}
        if self.records and (self.retention_days is None or retention_days > self.retention_days):
            for record in self.records.values():
                record.update(offset=0, status="pending", seen={}, observed_at="", error_code="", retry_at="")
        self.retention_days = retention_days
        for key in keys:
            record = self.records.setdefault(key, {"offset": 0, "status": "pending", "seen": {},
                "observed_at": "", "error_code": "", "retry_at": ""})
            record["seen"] = {identifier: value for identifier, value in record["seen"].items()
                              if valid_timestamp(value) and valid_timestamp(value) >= cutoff}

    def observe(self, key: str, timestamp: datetime) -> None:
        record = self.records[key]
        value = timestamp.isoformat()
        if record["observed_at"] != value:
            record.update(observed_at=value, status="pending")

    def batch(self, size: int) -> list[str]:
        keys = sorted(self.records)
        ordered = [key for key in keys if key > self.cursor] + [key for key in keys if key <= self.cursor]
        return [key for key in ordered if not self.records[key]["retry_at"]
                or valid_timestamp(self.records[key]["retry_at"]) <= utc_now()][:size]

    def start(self, key: str) -> int:
        self.cursor = key
        record = self.records[key]
        return max(0, record["offset"] - 1) if record["status"] == "captured" else record["offset"]

    def accept(self, key: str, offset: int, count: int, last: bool, seen: dict[str, str]) -> None:
        self.records[key].update(offset=offset + count, status="captured" if last else "pending",
                                 error_code="", retry_at="")
        self.records[key]["seen"].update(seen)

    def fail(self, key: str, code: str, retry_at: str = "") -> None:
        self.records[key].update(status="failed", error_code=code, retry_at=retry_at)

    def checkpoint(self) -> dict:
        return copy.deepcopy({"version": 1, "scope": self.scope, "retention_days": self.retention_days,
                              "cursor": self.cursor, "retry_at": self.retry_at, "records": self.records})
