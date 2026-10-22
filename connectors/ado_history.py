from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from typing import Any


class AdoHistory:
    """Minimal, account-scoped paging state; never stores raw work-item updates.

    ADO's update ID, revision, and list position are independent. Only the number
    of rows actually accepted advances a paging offset. A one-row overlap at the
    end of a pass plus update-ID deduplication makes retries harmless.
    """

    def __init__(self, scope: str, saved: dict | None = None):
        saved = saved or {}
        if saved.get("version") != 1 or saved.get("scope") != scope:
            saved = {}
        self.scope = scope
        retention = saved.get("retention_days")
        self.retention_days = retention if isinstance(retention, int) and not isinstance(retention, bool) and retention > 0 else None
        self.cursor = saved.get("cursor") if isinstance(saved.get("cursor"), str) else ""
        self.records: dict[str, dict[str, Any]] = {}
        records = saved.get("records")
        for key, record in records.items() if isinstance(records, dict) else []:
            if not isinstance(key, str) or not key.isdigit() or not isinstance(record, dict):
                continue
            offset = record.get("offset", 0)
            if not isinstance(offset, int) or offset < 0:
                continue
            seen = record.get("seen", {})
            retry_offset = record.get("retry_offset")
            self.records[key] = {
                "offset": offset,
                "retry_offset": retry_offset if isinstance(retry_offset, int) and not isinstance(retry_offset, bool) and retry_offset >= 0 else None,
                "status": record.get("status") if record.get("status") in {"pending", "captured", "failed", "unavailable", "deleted", "inaccessible"} else "pending",
                "seen": {str(identifier): timestamp for identifier, timestamp in seen.items()
                         if str(identifier).isdigit() and isinstance(timestamp, str)} if isinstance(seen, dict) else {},
                "latest_changed_date": str(record.get("latest_changed_date") or ""),
                "observed_at": str(record.get("observed_at") or ""),
                "undated": [identifier for identifier in record.get("undated", [])
                            if isinstance(identifier, int) and not isinstance(identifier, bool) and identifier > 0]
                           if isinstance(record.get("undated"), list) else [],
                **self.sanitize_diagnostic(record),
            }
        metadata = saved.get("field_metadata")
        self.field_metadata = self.sanitize_metadata(metadata)
        self.metadata_at = saved.get("metadata_at") if isinstance(saved.get("metadata_at"), str) else ""
        self.metadata_diagnostic = self.sanitize_diagnostic(saved.get("metadata_diagnostic"))
        self.inventory_diagnostic = self.sanitize_diagnostic(saved.get("inventory_diagnostic"))

    @staticmethod
    def sanitize_diagnostic(value: Any) -> dict:
        """Persist only fixed diagnostic tokens and timestamps, never messages or URLs."""
        value = value if isinstance(value, dict) else {}
        code = value.get("error_code")
        failures = value.get("consecutive_failures", 0)
        diagnostic = {
            "error_code": code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,80}", code) else "",
            "failure_category": value.get("failure_category") if value.get("failure_category") in {
                "transient", "deleted", "inaccessible", "invalid_response", "read_error"
            } else "",
            "last_failure_at": "", "retry_at": "",
            "consecutive_failures": min(failures, 30) if isinstance(failures, int) and not isinstance(failures, bool) and failures >= 0 else 0,
        }
        for name in ("last_failure_at", "retry_at"):
            try:
                timestamp = datetime.fromisoformat(value.get(name, "").replace("Z", "+00:00"))
                if timestamp.tzinfo is not None:
                    diagnostic[name] = timestamp.isoformat()
            except (ValueError, TypeError, AttributeError):
                pass
        return diagnostic

    @staticmethod
    def due(record: dict, now: datetime | None = None) -> bool:
        try:
            retry = datetime.fromisoformat(record.get("retry_at", "").replace("Z", "+00:00"))
            return retry.tzinfo is None or retry <= (now or datetime.now(timezone.utc))
        except (ValueError, TypeError, AttributeError):
            return True

    @staticmethod
    def sanitize_metadata(value: Any) -> dict[str, dict[str, str]]:
        if not isinstance(value, dict):
            return {}
        return {
            reference[:200]: {"name": str(field.get("name") or reference)[:200],
                              "type": str(field.get("type") or "")[:40]}
            for reference, field in value.items()
            if isinstance(reference, str) and isinstance(field, dict)
        }

    def prepare(self, keys: set[str], cutoff: datetime) -> None:
        self.records = {key: record for key, record in self.records.items() if key in keys}
        for key in keys:
            record = self.records.setdefault(key, {
                "offset": 0, "retry_offset": None, "status": "pending", "seen": {},
                "latest_changed_date": "", "observed_at": "", "undated": [], **self.sanitize_diagnostic({}),
            })
            # ISO timestamps are parsed defensively; old IDs need not occupy the
            # checkpoint after their corresponding activities expire.
            retained = {}
            for identifier, timestamp in record["seen"].items():
                try:
                    if datetime.fromisoformat(timestamp.replace("Z", "+00:00")) >= cutoff:
                        retained[identifier] = timestamp
                except (ValueError, TypeError):
                    continue
            record["seen"] = retained

    def set_retention_days(self, days: int) -> None:
        # Earlier scans deliberately omitted expired events. A wider window (or
        # an older checkpoint whose window is unknown) must revisit positions
        # already passed; stable update IDs let Store enrich without duplicates.
        if self.records and (self.retention_days is None or days > self.retention_days):
            for record in self.records.values():
                record.update(offset=0, retry_offset=None, seen={}, status="pending", undated=[], latest_changed_date="", **self.sanitize_diagnostic({}))
        self.retention_days = days

    def observe(self, key: str, timestamp: datetime, *, currently_visible: bool = False) -> None:
        record = self.records[key]
        current = timestamp.isoformat()
        if record["observed_at"] != current or (currently_visible and record["status"] == "deleted"):
            record["status"] = "pending"
            record["observed_at"] = current
            record.update(self.sanitize_diagnostic({}))

    def batch(self, size: int) -> list[str]:
        keys = sorted(key for key, record in self.records.items() if self.due(record))
        return ([key for key in keys if key > self.cursor] +
                [key for key in keys if key <= self.cursor])[:max(1, size)]

    def start(self, key: str) -> int:
        self.cursor = key
        record = self.records[key]
        # Retry undated gaps anywhere in the list, not just the final overlap.
        # Retained IDs still deduplicate events throughout this bounded rescan.
        if record["status"] == "unavailable":
            return 0
        if record.get("retry_offset") is not None:
            return record["retry_offset"]
        # Caught-up items are rechecked on each rotation, including unchanged
        # revisions; relation/admin updates can share a work-item revision.
        return max(0, record["offset"] - 1) if record["status"] == "captured" else record["offset"]

    def accept(self, key: str, offset: int, row_count: int, page_size: int,
               seen: dict[str, str], latest_changed_date: str = "", undated: set[int] | None = None,
               resolved: set[int] | None = None) -> None:
        record = self.records[key]
        record["offset"] = offset + row_count
        record["retry_offset"] = None
        record["seen"].update(seen)
        record["status"] = "pending" if row_count >= page_size else "captured"
        record["undated"] = sorted((set(record["undated"]) | (undated or set())) - (resolved or set()))
        if record["status"] == "captured" and record["undated"]:
            record["status"] = "unavailable"
        if not row_count and not record["offset"]:
            record["status"] = "unavailable"
        record.update(self.sanitize_diagnostic({}))
        if latest_changed_date:
            record["latest_changed_date"] = max(record["latest_changed_date"], latest_changed_date)

    def fail(self, key: str, code: str, *, category: str = "read_error", retry_seconds: float = 0,
             retry_offset: int | None = None) -> None:
        record = self.records[key]
        now = datetime.now(timezone.utc)
        record["status"] = category if category in {"deleted", "inaccessible"} else "failed"
        record["retry_offset"] = retry_offset if retry_offset is not None else record.get("retry_offset")
        record.update(self.sanitize_diagnostic({
            "error_code": code, "failure_category": category,
            "last_failure_at": now.isoformat(),
            "retry_at": (now + timedelta(seconds=retry_seconds)).isoformat() if retry_seconds > 0 else "",
            "consecutive_failures": record.get("consecutive_failures", 0) + 1,
        }))

    def checkpoint(self) -> dict:
        return copy.deepcopy({
            "version": 1, "scope": self.scope, "cursor": self.cursor,
            "retention_days": self.retention_days,
            "records": self.records, "field_metadata": self.field_metadata,
            "metadata_at": self.metadata_at,
            "metadata_diagnostic": self.metadata_diagnostic,
            "inventory_diagnostic": self.inventory_diagnostic,
        })
