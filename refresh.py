from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from config import Settings
from connectors import AzureDevOpsConnector, JiraConnector
from models import ConnectorHealth, ConnectorResult
from relevance import build_dashboard
from safety import safe_error
from store import Store


class RefreshCoordinator:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self.connectors = [
            AzureDevOpsConnector(settings.azure_devops, settings.app.connector_timeout_seconds),
            JiraConnector(settings.jira, settings.app.connector_timeout_seconds),
        ]

    async def refresh(self) -> None:
        if self._lock.locked():
            async with self._lock:
                return
        async with self._lock:
            results = await asyncio.gather(
                *(self._run_connector(connector) for connector in self.connectors)
            )
            for result in results:
                if result is not None:
                    if result.health.state in {"ok", "partial"}:
                        self.store.replace_connector(result, self.settings.app.activity_retention_days)
                    else:
                        self.store.record_health(result.health)

    async def _run_connector(self, connector) -> ConnectorResult | None:
        try:
            result = await asyncio.wait_for(
                connector.refresh(), timeout=self.settings.app.connector_timeout_seconds + 5
            )
            return result
        except TimeoutError:
            health = ConnectorHealth(
                connector=connector.name,
                state="error",
                message="Refresh timed out; showing the last successful snapshot",
                last_attempt_at=datetime.now(timezone.utc),
                error_code="TIMEOUT",
            )
        except Exception as exc:  # defensive boundary between independent connectors
            health = ConnectorHealth(
                connector=connector.name,
                state="error",
                message="Connector failed; showing the last successful snapshot",
                last_attempt_at=datetime.now(timezone.utc),
                error_code=exc.__class__.__name__.upper(),
                coverage={"diagnostic": safe_error(exc)},
            )
        self.store.record_health(health)
        return None

    async def run_periodic(self) -> None:
        while not self._stop.is_set():
            await self.refresh()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.app.refresh_seconds
                )
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()

    def dashboard(self) -> dict:
        work_items, activities, health = self.store.load()
        return build_dashboard(
            work_items, activities, health, self.settings.app.stale_after_days
        )
