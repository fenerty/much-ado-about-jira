from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from models import Activity, ConnectorHealth, WorkItem


HIGH_REASONS = {"mentioned", "replied", "reviewer", "requested_changes"}
FOLLOW_REASONS = {"watching", "participant", "author", "waiting", "previously_assigned"}
REASON_WEIGHT = {
    "mentioned": 100,
    "replied": 95,
    "reviewer": 90,
    "requested_changes": 88,
    "assigned": 70,
    "waiting": 50,
    "author": 45,
    "watching": 35,
    "participant": 30,
}


def _timestamp(value: datetime) -> float:
    return value.timestamp()


def _rank(item: WorkItem) -> tuple[int, int, float]:
    reason_score = max((REASON_WEIGHT.get(reason, 0) for reason in item.reasons), default=0)
    risk = int(item.status_category == "blocked") * 20
    risk += int(item.metadata.get("failed_checks", 0) > 0) * 15
    risk += int(item.metadata.get("unresolved_threads", 0) > 0) * 12
    return (int(item.unread) * 30 + reason_score + risk, int(item.actionable), _timestamp(item.updated_at))


def _entry_from_work(item: WorkItem, stale_after_days: int) -> dict[str, Any]:
    payload = item.model_dump(mode="json")
    payload["entity_kind"] = "work_item"
    payload["stale"] = item.updated_at < datetime.now(timezone.utc) - timedelta(days=stale_after_days)
    return payload


def _entry_from_activity(activity: Activity) -> dict[str, Any]:
    payload = activity.model_dump(mode="json")
    payload["entity_kind"] = "activity"
    payload["title"] = activity.item_title
    payload["key"] = activity.item_key
    payload["updated_at"] = payload["timestamp"]
    payload["actionable"] = bool(set(activity.reasons) & HIGH_REASONS)
    return payload


def build_dashboard(
    work_items: list[WorkItem],
    activities: list[Activity],
    health: list[ConnectorHealth],
    stale_after_days: int,
) -> dict[str, Any]:
    sorted_work = sorted(work_items, key=_rank, reverse=True)
    sorted_activity = sorted(activities, key=lambda item: item.timestamp, reverse=True)
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_after_days)

    attention_work = [
        item
        for item in sorted_work
        if item.actionable
        or (item.unread and "assigned" in item.reasons)
    ]
    stale_cleanup = [
        item
        for item in sorted_work
        if "assigned" in item.reasons
        and item.updated_at < stale_cutoff
        and item not in attention_work
    ][:5]
    attention_work.extend(stale_cleanup)
    attention_activity = [
        item for item in sorted_activity if item.unread and set(item.reasons) & HIGH_REASONS
    ]
    needs_attention = [_entry_from_activity(item) for item in attention_activity]
    needs_attention.extend(_entry_from_work(item, stale_after_days) for item in attention_work)
    needs_attention.sort(
        key=lambda entry: (int(entry.get("unread", False)), entry.get("updated_at", "")),
        reverse=True,
    )

    assigned = [
        item
        for item in sorted_work
        if "assigned" in item.reasons and item.source_type != "pull_request" and item.status_category != "done" and not item.metadata.get("snapshot_only")
    ]
    my_work: dict[str, list[dict[str, Any]]] = {
        "in_progress": [],
        "todo": [],
        "blocked": [],
        "other": [],
    }
    for item in assigned:
        group = item.status_category if item.status_category in my_work else "other"
        my_work[group].append(_entry_from_work(item, stale_after_days))

    pull_requests = [item for item in sorted_work if item.source_type == "pull_request" and item.status_category != "done" and not item.metadata.get("snapshot_only")]
    code = {
        "awaiting_review": [
            _entry_from_work(item, stale_after_days)
            for item in pull_requests
            if "reviewer" in item.reasons and item.metadata.get("reviewer_vote", 0) == 0 and item.status != "Draft"
        ],
        "reviewing": [
            _entry_from_work(item, stale_after_days)
            for item in pull_requests if "reviewer" in item.reasons
        ],
        "mine": [
            _entry_from_work(item, stale_after_days)
            for item in pull_requests
            if "author" in item.reasons
        ],
        "discussions": [
            _entry_from_work(item, stale_after_days)
            for item in pull_requests
            if {"participant", "replied", "mentioned"} & set(item.reasons)
        ],
    }

    following = [
        _entry_from_work(item, stale_after_days)
        for item in sorted_work
        if set(item.reasons) & FOLLOW_REASONS and "assigned" not in item.reasons
    ]

    health_by_name = {item.connector: item.model_dump(mode="json") for item in health}
    for connector in ("azure_devops", "jira"):
        health_by_name.setdefault(
            connector,
            ConnectorHealth(
                connector=connector,
                state="loading",
                message="Waiting for the first refresh",
            ).model_dump(mode="json"),
        )

    return {
        "tracked_items": [_entry_from_work(item, stale_after_days) for item in sorted_work],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "assigned": len(assigned),
            "pr_reviews": len(code["awaiting_review"]),
            "new_mentions_replies": sum(
                1
                for item in sorted_activity
                if item.unread and {"mentioned", "replied"} & set(item.reasons)
            ),
            "following_waiting": len(following),
            "stale": sum(
                1
                for item in assigned
                if item.updated_at < stale_cutoff
            ),
        },
        "needs_attention": needs_attention,
        "my_work": my_work,
        "code": code,
        "activity": [_entry_from_activity(item) for item in sorted_activity],
        "following_waiting": following,
        "health": health_by_name,
    }
