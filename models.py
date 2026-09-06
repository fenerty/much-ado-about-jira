from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


Source = Literal["ado", "azure_repos", "jira"]
HealthState = Literal["ok", "partial", "auth_required", "error", "disabled", "loading"]
StatusCategory = Literal["todo", "in_progress", "blocked", "done", "other"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkItem(BaseModel):
    id: str
    source: Source
    source_type: str
    project: str
    repository: str | None = None
    key: str
    title: str
    url: str
    status: str
    status_category: StatusCategory = "other"
    priority: str | None = None
    assigned_to: str | None = None
    author: str | None = None
    updated_at: datetime
    created_at: datetime | None = None
    actionable: bool = False
    reasons: list[str] = Field(default_factory=list)
    unread: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class Activity(BaseModel):
    id: str
    source: Source
    event_type: str
    actor: str | None = None
    item_id: str
    item_key: str
    item_title: str
    timestamp: datetime
    summary: str
    url: str
    reasons: list[str] = Field(default_factory=list)
    unread: bool = False


class ConnectorHealth(BaseModel):
    connector: Literal["azure_devops", "jira"]
    state: HealthState
    message: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    error_code: str | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)


class ConnectorResult(BaseModel):
    connector: Literal["azure_devops", "jira"]
    work_items: list[WorkItem] = Field(default_factory=list)
    activities: list[Activity] = Field(default_factory=list)
    health: ConnectorHealth
