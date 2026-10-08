from __future__ import annotations

import asyncio
import logging
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
        self._persistence_errors: dict[str, ConnectorHealth] = {}
        self.connectors = [
            AzureDevOpsConnector(settings.azure_devops, settings.app.connector_timeout_seconds),
            JiraConnector(settings.jira, settings.app.connector_timeout_seconds),
        ]
        self.connectors[1].state_loader = lambda cache_scope: self.store.load_connector_state(
            'jira', cache_scope=cache_scope
        )

    async def refresh(self) -> None:
        if self._lock.locked():
            async with self._lock:
                return
        async with self._lock:
            results = await asyncio.gather(
                *(self._run_connector(connector) for connector in self.connectors)
            )
            failures = []
            for result in results:
                if result is None:
                    continue
                try:
                    if result.health.state in {"ok", "partial"}:
                        self.store.replace_connector(result, self.settings.app.activity_retention_days)
                        if result.connector == "jira":
                            jira = next(connector for connector in self.connectors if connector.name == "jira")
                            self.store.save_connector_state("jira", jira.checkpoint())
                    else:
                        self.store.record_health(result.health)
                except Exception as exc:
                    # Keep failures visible even when SQLite cannot accept a health write.
                    self._persistence_errors[result.connector] = ConnectorHealth(
                        connector=result.connector, state='error',
                        message='Local cache or refresh progress could not be saved; automatic refresh will retry',
                        last_attempt_at=datetime.now(timezone.utc), error_code='CACHE_WRITE_FAILED',
                        coverage={'diagnostic': safe_error(exc)},
                    )
                    failures.append(exc)
                else:
                    self._persistence_errors.pop(result.connector, None)
            if failures:
                raise failures[0]

    async def _run_connector(self, connector) -> ConnectorResult | None:
        try:
            if isinstance(connector, JiraConnector):
                connector.load_state(self.store.load_connector_state("jira"))
            timeout = (self.settings.jira.refresh_timeout_seconds if connector.name == "jira"
                       else self.settings.app.connector_timeout_seconds) + 5
            result = await asyncio.wait_for(
                connector.refresh(), timeout=timeout
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
        return ConnectorResult(connector=connector.name, health=health)

    async def run_periodic(self) -> None:
        while not self._stop.is_set():
            try:
                await self.refresh()
            except Exception:
                # A transient storage failure must not terminate automatic syncing.
                logging.exception('Dashboard refresh failed; retrying after the refresh interval')
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
        health_by_name = {entry.connector: entry for entry in health}
        for name, failure in self._persistence_errors.items():
            previous = health_by_name.get(name)
            health_by_name[name] = failure.model_copy(update={
                'last_success_at': previous.last_success_at if previous else None,
            })
        health = list(health_by_name.values())
        dashboard = build_dashboard(
            work_items, activities, health, self.settings.app.stale_after_days
        )
        dashboard["dismissed"] = self.store.load_dismissed()
        dashboard['collection_windows'] = {'jira_comment_discovery_enabled': bool(self.settings.jira.activity_projects), 'jira_mentions_days': self.settings.jira.mention_reply_days, 'jira_participation_days': self.settings.jira.participation_days, 'ado_mentions_days': self.settings.azure_devops.mention_reply_days, 'jira_query_cap': self.settings.jira.max_candidates_per_query, 'ado_item_cap': self.settings.azure_devops.max_work_items, 'ado_comment_cap': self.settings.azure_devops.max_comment_candidates}
        progress = dashboard['health'].get('jira', {}).get('coverage', {}).get('completed_history')
        if progress:
            progress['eta_seconds'] = progress['batches_remaining'] * (self.settings.app.refresh_seconds + progress.get('last_sync_seconds', 0))
        dashboard["stale_after_days"] = self.settings.app.stale_after_days
        dashboard["activity_retention_days"] = self.settings.app.activity_retention_days
        return dashboard
