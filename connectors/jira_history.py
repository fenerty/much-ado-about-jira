from __future__ import annotations

import copy


HISTORY_ROLES = ("assigned", "previously_assigned", "watching", "author")


class JiraHistory:
    """Account-scoped discovery cursors and minimal, durable hydration inventory."""

    def __init__(self, scope: str, saved: dict | None = None):
        saved = saved or {}
        if saved.get("version") != 1 or saved.get("scope") != scope:
            saved = {}
        self.scope = scope
        self.cursors = {role: "" for role in HISTORY_ROLES}
        self.passes = {role: 0 for role in HISTORY_ROLES}
        self.records: dict[str, set[str]] = {}
        records = saved.get("records", {})
        for key, roles in records.items() if isinstance(records, dict) else []:
            if isinstance(key, str) and isinstance(roles, list):
                valid = set(role for role in roles if isinstance(role, str) and role in HISTORY_ROLES)
                if valid:
                    self.records[key] = valid
        for role in HISTORY_ROLES:
            cursor = saved.get("cursors", {}).get(role) if isinstance(saved.get("cursors"), dict) else None
            if isinstance(cursor, str):
                self.cursors[role] = cursor
            count = saved.get("passes", {}).get(role) if isinstance(saved.get("passes"), dict) else None
            if isinstance(count, int) and count >= 0:
                self.passes[role] = count
        checked = saved.get("checked", [])
        self.checked = {key for key in checked if isinstance(key, str) and key in self.records} if isinstance(checked, list) else set()
        index = saved.get("next_role", 0)
        self.next_role = index % len(HISTORY_ROLES) if isinstance(index, int) else 0
        cursor = saved.get("hydration_cursor", "")
        self.hydration_cursor = cursor if isinstance(cursor, str) else ""

    def accept_page(self, role: str, keys: list[str], full: bool) -> None:
        if any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("History search returned invalid or duplicate keys")
        if keys and keys[-1] == self.cursors[role]:
            raise ValueError("History search did not advance its cursor")
        for key in keys:
            self.records.setdefault(key, set()).add(role)
        # Jira owns key ordering (ENG-9 precedes ENG-10). Use its last row.
        if full and keys:
            self.cursors[role] = keys[-1]
        else:
            self.cursors[role] = ""
            self.passes[role] += 1

    def hydration_batch(self, size: int, *, verified: set[str] | None = None) -> list[str]:
        self.checked &= self.records.keys()
        if self.records and len(self.checked) == len(self.records):
            self.checked.clear()
        self.checked.update((verified or set()) & self.records.keys())
        pending = sorted(self.records.keys() - self.checked)
        # Failed keys remain retryable, but cannot monopolize the first batch.
        pending = [key for key in pending if key > self.hydration_cursor] + [key for key in pending if key <= self.hydration_cursor]
        batch = pending[:size]
        if batch:
            self.hydration_cursor = batch[-1]
        return batch

    def checkpoint(self) -> dict:
        return copy.deepcopy({
            "version": 1, "scope": self.scope, "cursors": self.cursors,
            "passes": self.passes, "next_role": self.next_role,
            "records": {key: sorted(roles) for key, roles in self.records.items()},
            "checked": sorted(self.checked), "hydration_cursor": self.hydration_cursor,
        })
