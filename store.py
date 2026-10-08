from __future__ import annotations

import hashlib
import json
import uuid
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from activity_details import snapshot_changes
from config import source_realm
from models import Activity, ConnectorHealth, ConnectorResult, WorkItem, utc_now


class Store:
    def __init__(self, database_path: Path, cache_bindings: dict[str, dict] | None = None):
        self.database_path = database_path
        self._lock = threading.RLock()
        self._cache_bindings = None if cache_bindings is None else {
            name: dict(binding) for name, binding in cache_bindings.items()
        }
        self._active_connectors = {} if self._cache_bindings is None else {
            name: f"{name}@{binding['scope']}" for name, binding in self._cache_bindings.items()
        }

    def _storage_connector(self, connector: str) -> str | None:
        return connector if self._cache_bindings is None else self._active_connectors.get(connector)

    def _entity_id(self, connector: str, raw_id: str) -> str:
        return raw_id if self._cache_bindings is None else f"{connector}:{raw_id}"

    def _scope_filter(self, alias: str = "") -> tuple[str, tuple]:
        if self._cache_bindings is None:
            return "1 = 1", ()
        connectors = tuple(self._active_connectors.values())
        if not connectors:
            return "0 = 1", ()
        column = f"{alias}.connector" if alias else "connector"
        return f"{column} IN ({','.join('?' for _ in connectors)})", connectors

    def _verified_connector(self, connector: str, cache_scope: str | None) -> str:
        if self._cache_bindings is None:
            return connector
        binding = self._cache_bindings.get(connector)
        if binding is None or not cache_scope or cache_scope.startswith('unverified:'):
            raise ValueError("A configured connector and verified cache scope are required")
        if not binding['scope'].startswith('unverified:') and cache_scope != binding['scope']:
            raise ValueError("Verified cache scope does not match the configured source/account")
        return f"{connector}@{cache_scope}"

    def activate_connector_scope(self, connector: str, cache_scope: str) -> None:
        """Switch visibility after authentication, independently of cache writes."""
        with self._lock:
            storage = self._verified_connector(connector, cache_scope)
            if self._cache_bindings is not None:
                self._active_connectors[connector] = storage

    def _row_data(self, row: sqlite3.Row) -> dict:
        data = json.loads(row['payload_json'])
        data['id'] = row['entity_id']
        if row['entity_kind'] == 'activity':
            data['item_id'] = self._entity_id(row['connector'], data['item_id'])
        return data

    def _adopt_legacy(self, connection: sqlite3.Connection) -> None:
        """Adopt only a first Jira snapshot with verified saved identity proof."""
        if self._cache_bindings is None:
            return
        connection.execute('BEGIN IMMEDIATE')
        for connector, binding in self._cache_bindings.items():
            # Old ADO health could contain a display name rather than the login
            # account. Preserve those rows without assigning them an identity.
            if connector != 'jira':
                continue
            account = str(binding.get('account') or '').strip().lower()
            if not account or binding['scope'].startswith('unverified:'):
                continue
            storage = self._active_connectors[connector]
            prefix = storage + ':'
            if (connection.execute('SELECT 1 FROM entities WHERE connector = ? LIMIT 1', (storage,)).fetchone()
                    or connection.execute('SELECT 1 FROM local_state WHERE substr(entity_id, 1, ?) = ? LIMIT 1', (len(prefix), prefix)).fetchone()
                    or connection.execute('SELECT 1 FROM connector_runs WHERE connector = ?', (storage,)).fetchone()
                    or connection.execute('SELECT 1 FROM app_meta WHERE key IN (?, ?)', (f'baseline:{storage}', f'connector_state:{storage}')).fetchone()):
                continue
            previous = connection.execute('SELECT health_json FROM connector_runs WHERE connector = ?', (connector,)).fetchone()
            baseline = connection.execute('SELECT value FROM app_meta WHERE key = ?', (f'baseline:{connector}',)).fetchone()
            rows = connection.execute('SELECT entity_id, entity_kind, active, first_seen_at, last_seen_at FROM entities WHERE connector = ?', (connector,)).fetchall()
            # A latest health/checkpoint cannot prove older retained rows belong to
            # the same source/account. Only an untouched first snapshot is safe.
            if (previous is None or baseline is None or not rows
                    or any(row['first_seen_at'] != baseline['value'] or row['last_seen_at'] != baseline['value']
                           or (row['entity_kind'] == 'work_item' and not row['active']) for row in rows)):
                continue
            try:
                coverage = json.loads(previous['health_json']).get('coverage', {})
                if not isinstance(coverage, dict):
                    continue
                checkpoint = connection.execute('SELECT value FROM app_meta WHERE key = ?', (f'connector_state:{connector}',)).fetchone()
                state = json.loads(checkpoint['value']) if checkpoint else {}
                proven = (source_realm(connector, str(coverage.get('site') or '')) == binding['realm']
                          and isinstance(state, dict) and bool(binding.get('legacy_history_scope'))
                          and state.get('scope') == binding['legacy_history_scope'])
            except (ValueError, TypeError, AttributeError):
                continue
            if not proven:
                continue
            for row in rows:
                qualified = self._entity_id(storage, row['entity_id'])
                connection.execute('UPDATE local_state SET entity_id = ? WHERE entity_id = ?', (qualified, row['entity_id']))
                connection.execute('UPDATE entities SET entity_id = ?, connector = ? WHERE entity_id = ?', (qualified, storage, row['entity_id']))
            connection.execute('UPDATE connector_runs SET connector = ? WHERE connector = ?', (storage, connector))
            for category in ('baseline', 'connector_state'):
                connection.execute('UPDATE app_meta SET key = ? WHERE key = ?', (f'{category}:{storage}', f'{category}:{connector}'))

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS entities (
                    entity_id TEXT PRIMARY KEY,
                    entity_kind TEXT NOT NULL CHECK(entity_kind IN ('work_item', 'activity')),
                    connector TEXT NOT NULL,
                    source TEXT NOT NULL,
                    version_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    event_at TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_entities_active
                    ON entities(entity_kind, active, event_at DESC);
                CREATE INDEX IF NOT EXISTS idx_entities_connector
                    ON entities(connector, entity_kind, active);
                CREATE TABLE IF NOT EXISTS local_state (
                    entity_id TEXT PRIMARY KEY,
                    seen_version TEXT,
                    dismissed_version TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connector_runs (
                    connector TEXT PRIMARY KEY,
                    health_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._adopt_legacy(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def load_connector_state(self, connector: str, *, cache_scope: str | None = None) -> dict:
        """Read active progress, or a verified namespace without activating it."""
        with self._lock, self._connect() as connection:
            storage = (self._verified_connector(connector, cache_scope) if cache_scope is not None
                       else self._storage_connector(connector))
            if storage is None:
                return {}
            row = connection.execute(
                "SELECT value FROM app_meta WHERE key = ?",
                (f"connector_state:{storage}",),
            ).fetchone()
        if row is None:
            return {}
        try:
            state = json.loads(row["value"])
        except (ValueError, TypeError):
            return {}
        return state if isinstance(state, dict) else {}

    def save_connector_state(self, connector: str, state: dict) -> None:
        """Persist refresh progress independently of entities and local triage."""
        if not isinstance(state, dict):
            raise TypeError("Connector state must be a dictionary")
        payload = json.dumps(state, sort_keys=True)
        with self._lock, self._connect() as connection:
            storage = self._storage_connector(connector)
            if storage is None:
                raise ValueError("Connector is not configured")
            connection.execute(
                """
                INSERT INTO app_meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"connector_state:{storage}", payload),
            )

    @staticmethod
    def _activity_version(raw: dict) -> str:
        # Relationships describe why an event is relevant now, not what changed
        # in the source. Lookback expiry must not reset read or hidden state.
        content = {key: value for key, value in raw.items() if key not in {'reasons', 'unread'}}
        payload = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _payload(model: WorkItem | Activity) -> tuple[str, str]:
        raw = model.model_dump(mode="json")
        raw["unread"] = False
        payload = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        version = (Store._activity_version(raw) if isinstance(model, Activity)
                   else hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24])
        return payload, version

    def replace_connector(self, result: ConnectorResult, retention_days: int) -> None:
        now = utc_now().isoformat()
        storage = self._verified_connector(result.connector, result.cache_scope)
        baseline_key = f"baseline:{storage}"
        with self._lock:
            with self._connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                baseline = connection.execute(
                    "SELECT value FROM app_meta WHERE key = ?", (baseline_key,)
                ).fetchone()

                connection.execute(
                    "UPDATE entities SET active = 0 WHERE connector = ? AND entity_kind = 'work_item'",
                    (storage,),
                )
                previous_items = {
                    json.loads(row['payload_json'])['id']: json.loads(row['payload_json'])
                    for row in connection.execute("SELECT entity_id, payload_json FROM entities WHERE connector = ? AND entity_kind = 'work_item'", (storage,))
                }
                for item in result.work_items:
                    previous = previous_items.get(item.id, {})
                    historical = set(previous.get('metadata', {}).get('tracked_relationships', [])) | set(previous.get('reasons', [])) | set(item.reasons)
                    item.metadata['tracked_relationships'] = sorted(historical)
                    if 'assigned' in historical and 'assigned' not in item.reasons and 'previously_assigned' not in item.reasons:
                        item.reasons.append('previously_assigned')
                current_items = {item.id: item.model_dump(mode='json') for item in result.work_items}
                for item in result.work_items:
                    self._upsert_entity(connection, storage, "work_item", item, item.updated_at, now)
                for activity in result.activities:
                    if activity.item_id not in previous_items and activity.item_id not in current_items:
                        parent = WorkItem(id=activity.item_id, source=activity.source, source_type='unknown', project='', key=activity.item_key, title=activity.item_title, url=activity.url, status='Unknown', updated_at=activity.timestamp, reasons=activity.reasons, metadata={'discovered_from_update': True})
                        self._upsert_entity(connection, storage, 'work_item', parent, parent.updated_at, now)
                        connection.execute('UPDATE entities SET active = 0 WHERE entity_id = ?', (self._entity_id(storage, parent.id),))
                        previous_items[parent.id] = parent.model_dump(mode='json')
                    if activity.event_type == 'updated' and not activity.changes:
                        existing = connection.execute("SELECT payload_json FROM entities WHERE entity_id = ? AND entity_kind = 'activity'", (self._entity_id(storage, activity.id),)).fetchone()
                        saved = json.loads(existing['payload_json']) if existing else {}
                        if saved.get('changes'):
                            activity = activity.model_copy(update={key: saved.get(key) for key in ('changes', 'detail_source', 'summary', 'actor')})
                        elif activity.item_id in previous_items and activity.item_id in current_items:
                            before, after = previous_items[activity.item_id], current_items[activity.item_id]
                            if before.get('updated_at') != after.get('updated_at'):
                                changes = snapshot_changes(before, after)
                                if changes:
                                    activity = activity.model_copy(update={'changes': changes, 'detail_source': 'Between local refreshes', 'summary': '; '.join(changes)})
                    self._upsert_entity(
                        connection, storage, "activity", activity, activity.timestamp, now
                    )

                if baseline is None:
                    rows = connection.execute(
                        "SELECT entity_id, version_hash FROM entities WHERE connector = ? AND active = 1",
                        (storage,),
                    ).fetchall()
                    for row in rows:
                        connection.execute(
                            """
                            INSERT INTO local_state(entity_id, seen_version, dismissed_version, updated_at)
                            VALUES(?, ?, NULL, ?)
                            ON CONFLICT(entity_id) DO UPDATE SET
                                seen_version = excluded.seen_version,
                                updated_at = excluded.updated_at
                            """,
                            (row["entity_id"], row["version_hash"], now),
                        )
                    connection.execute(
                        "INSERT INTO app_meta(key, value) VALUES(?, ?)", (baseline_key, now)
                    )

                cutoff = (utc_now() - timedelta(days=retention_days)).isoformat()
                if self._cache_bindings is None:
                    connection.execute("DELETE FROM entities WHERE entity_kind = 'activity' AND event_at < ?", (cutoff,))
                    connection.execute("DELETE FROM local_state WHERE entity_id NOT IN (SELECT entity_id FROM entities)")
                else:
                    connection.execute("DELETE FROM local_state WHERE entity_id IN (SELECT entity_id FROM entities WHERE connector = ? AND entity_kind = 'activity' AND event_at < ?)", (storage, cutoff))
                    connection.execute("DELETE FROM entities WHERE connector = ? AND entity_kind = 'activity' AND event_at < ?", (storage, cutoff))
                self._write_health(connection, result.health, storage)

            if self._cache_bindings is not None:
                self._active_connectors[result.connector] = storage

    def _upsert_entity(
        self,
        connection: sqlite3.Connection,
        connector: str,
        kind: Literal["work_item", "activity"],
        model: WorkItem | Activity,
        event_at: datetime,
        now: str,
    ) -> None:
        payload, version = self._payload(model)
        entity_id = self._entity_id(connector, model.id)
        if kind == 'activity':
            previous = connection.execute(
                "SELECT payload_json, version_hash FROM entities WHERE entity_id = ?",
                (entity_id,),
            ).fetchone()
            if previous and self._activity_version(json.loads(previous['payload_json'])) == version:
                # Reuse legacy full-payload versions until source content changes.
                # This preserves seen/dismissed versions and outstanding Undo tokens
                # without migrating or guessing previously acknowledged content.
                version = previous['version_hash']
        connection.execute(
            """
            INSERT INTO entities(
                entity_id, entity_kind, connector, source, version_hash, payload_json,
                event_at, first_seen_at, last_seen_at, active
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(entity_id) DO UPDATE SET
                connector = excluded.connector,
                source = excluded.source,
                version_hash = excluded.version_hash,
                payload_json = excluded.payload_json,
                event_at = excluded.event_at,
                last_seen_at = excluded.last_seen_at,
                active = 1
            """,
            (
                entity_id,
                kind,
                connector,
                model.source,
                version,
                payload,
                event_at.isoformat(),
                now,
                now,
            ),
        )

    def record_health(self, health: ConnectorHealth, *, cache_scope: str | None = None) -> None:
        if cache_scope is not None:
            self.activate_connector_scope(health.connector, cache_scope)
        with self._lock, self._connect() as connection:
            storage = self._storage_connector(health.connector)
            if storage is not None:
                self._write_health(connection, health, storage)

    @staticmethod
    def _write_health(connection: sqlite3.Connection, health: ConnectorHealth, connector: str | None = None) -> None:
        connector = connector or health.connector
        if health.last_success_at is None:
            previous = connection.execute(
                "SELECT health_json FROM connector_runs WHERE connector = ?",
                (connector,),
            ).fetchone()
            if previous is not None:
                try:
                    previous_health = ConnectorHealth.model_validate_json(previous["health_json"])
                except ValueError:
                    pass
                else:
                    health = health.model_copy(update={"last_success_at": previous_health.last_success_at})
        payload = json.dumps(health.model_dump(mode="json"), sort_keys=True)
        connection.execute(
            """
            INSERT INTO connector_runs(connector, health_json) VALUES(?, ?)
            ON CONFLICT(connector) DO UPDATE SET health_json = excluded.health_json
            """,
            (connector, payload),
        )

    def load(self) -> tuple[list[WorkItem], list[Activity], list[ConnectorHealth]]:
        with self._lock, self._connect() as connection:
            scope, scopes = self._scope_filter('e')
            health_scope, health_scopes = self._scope_filter()
            rows = connection.execute(
                f"""
                SELECT e.*, s.seen_version, s.dismissed_version
                FROM entities e
                LEFT JOIN local_state s ON s.entity_id = e.entity_id
                WHERE (e.active = 1 OR e.entity_kind = 'work_item') AND {scope}
                ORDER BY e.event_at DESC
                """, scopes
            ).fetchall()
            health_rows = connection.execute(
                f"SELECT health_json FROM connector_runs WHERE {health_scope} ORDER BY connector", health_scopes
            ).fetchall()

        work_items: list[WorkItem] = []
        activities: list[Activity] = []
        for row in rows:
            if row["dismissed_version"] == row["version_hash"] or (row['entity_kind'] == 'work_item' and row['dismissed_version'] is not None):
                continue
            data = self._row_data(row)
            data["unread"] = row["seen_version"] != row["version_hash"]
            if row["entity_kind"] == "work_item":
                data['metadata']['snapshot_only'] = not bool(row['active'])
                data['metadata']['last_verified_at'] = row['last_seen_at']
                work_items.append(WorkItem.model_validate(data))
            else:
                activities.append(Activity.model_validate(data))
        health = [ConnectorHealth.model_validate_json(row["health_json"]) for row in health_rows]
        return work_items, activities, health

    def load_dismissed(self) -> list[dict]:
        """Durably hidden work, including legacy hashes, and current hidden events."""
        with self._lock, self._connect() as connection:
            scope, scopes = self._scope_filter('e')
            rows = connection.execute(f"""
                SELECT e.entity_id, e.connector, e.payload_json, e.entity_kind, e.version_hash, e.active, s.seen_version, s.updated_at
                FROM entities e JOIN local_state s ON s.entity_id = e.entity_id
                WHERE (e.active = 1 OR e.entity_kind = 'work_item') AND {scope} AND (e.version_hash = s.dismissed_version OR (e.entity_kind = 'work_item' AND s.dismissed_version IS NOT NULL))
                ORDER BY s.updated_at DESC
            """, scopes).fetchall()
        return [{**self._row_data(row), 'entity_kind': row['entity_kind'],
                 'unread': row['seen_version'] != row['version_hash'],
                 'metadata': {**json.loads(row['payload_json']).get('metadata', {}), 'snapshot_only': not bool(row['active'])},
                 'dismissed_at': row['updated_at']} for row in rows]

    def hide_work(self, entity_id: str) -> list[dict]:
        """Hide inventory until restored; related updates remain independent."""
        with self._lock, self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            scope, scopes = self._scope_filter()
            if not connection.execute(f"SELECT 1 FROM entities WHERE entity_id = ? AND entity_kind = 'work_item' AND {scope}", (entity_id, *scopes)).fetchone():
                return []
            token = 'work:' + uuid.uuid4().hex
            connection.execute('''INSERT INTO local_state(entity_id, dismissed_version, updated_at)
                VALUES(?, ?, ?) ON CONFLICT(entity_id) DO UPDATE SET
                dismissed_version = excluded.dismissed_version, updated_at = excluded.updated_at''',
                (entity_id, token, utc_now().isoformat()))
            return [{'entity_id': entity_id, 'version': token}]

    def dismiss_updates(self, entity_id: str, all_for_item: bool = False) -> list[dict]:
        """Hide existing update events, never their parent item or future updates."""
        with self._lock, self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            scope, scopes = self._scope_filter()
            target = connection.execute(f"SELECT payload_json, connector FROM entities WHERE entity_id = ? AND entity_kind = 'activity' AND active = 1 AND {scope}", (entity_id, *scopes)).fetchone()
            if not target:
                return []
            item_id = json.loads(target['payload_json'])['item_id']
            rows = connection.execute("""SELECT e.entity_id, e.version_hash, e.payload_json
                FROM entities e LEFT JOIN local_state s ON s.entity_id = e.entity_id
                WHERE e.entity_kind = 'activity' AND e.active = 1 AND e.connector = ?
                AND (s.dismissed_version IS NULL OR s.dismissed_version != e.version_hash)""", (target['connector'],)).fetchall()
            undo = []
            for row in rows:
                if row['entity_id'] != entity_id and not (all_for_item and json.loads(row['payload_json'])['item_id'] == item_id):
                    continue
                connection.execute("""INSERT INTO local_state(entity_id, dismissed_version, updated_at)
                    VALUES(?, ?, ?) ON CONFLICT(entity_id) DO UPDATE SET
                    dismissed_version = excluded.dismissed_version, updated_at = excluded.updated_at""",
                    (row['entity_id'], row['version_hash'], utc_now().isoformat()))
                undo.append({'entity_id': row['entity_id'], 'version': row['version_hash']})
            return undo

    def restore_dismissed(self, entries: list[dict]) -> None:
        """Undo only the versions hidden by that action, preserving seen state."""
        with self._lock, self._connect() as connection:
            scope, scopes = self._scope_filter()
            for entry in entries:
                connection.execute(f"""UPDATE local_state SET dismissed_version = NULL
                    WHERE entity_id = ? AND dismissed_version = ?
                    AND entity_id IN (SELECT entity_id FROM entities WHERE {scope})""",
                    (entry['entity_id'], entry['version'], *scopes))

    def mark_batch(self, entity_ids: list[str], action: str) -> int:
        """Read an event plus its parent, or selected work plus its current events.

        Expand from the original selection only: reading one event must not
        recursively read its siblings unless seen_related is requested.
        Unread remains an explicit row action.
        """
        with self._lock, self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            scope, scopes = self._scope_filter()
            rows = connection.execute(f'SELECT entity_id, connector, entity_kind, version_hash, payload_json FROM entities WHERE {scope}', scopes).fetchall()
            by_id = {row['entity_id']: row for row in rows}
            selected = set(entity_ids) & by_id.keys()
            targets = set(selected)
            if action in {'seen', 'seen_related'}:
                selected_work = {entity_id for entity_id in selected if by_id[entity_id]['entity_kind'] == 'work_item'}
                for entity_id in selected:
                    row = by_id[entity_id]
                    if row['entity_kind'] == 'activity':
                        parent_id = self._entity_id(row['connector'], json.loads(row['payload_json'])['item_id'])
                        if action == 'seen_related':
                            selected_work.add(parent_id)
                        if parent_id in by_id and by_id[parent_id]['entity_kind'] == 'work_item':
                            targets.add(parent_id)
                for row in rows:
                    if row['entity_kind'] == 'activity' and self._entity_id(row['connector'], json.loads(row['payload_json'])['item_id']) in selected_work:
                        targets.add(row['entity_id'])
            now = utc_now().isoformat()
            for entity_id in targets:
                connection.execute('''INSERT INTO local_state(entity_id, seen_version, updated_at)
                    VALUES(?, ?, ?) ON CONFLICT(entity_id) DO UPDATE SET
                    seen_version = excluded.seen_version, updated_at = excluded.updated_at''',
                    (entity_id, by_id[entity_id]['version_hash'] if action in {'seen', 'seen_related'} else None, now))
            return len(selected)

    def set_local_state(self, entity_id: str, action: Literal["seen", "seen_related", "unread", "dismiss", "restore"]) -> bool:
        if action in {'seen', 'seen_related', 'unread'}:
            return bool(self.mark_batch([entity_id], action))
        with self._lock, self._connect() as connection:
            scope, scopes = self._scope_filter()
            row = connection.execute(
                f"SELECT version_hash FROM entities WHERE entity_id = ? AND (active = 1 OR entity_kind = 'work_item') AND {scope}", (entity_id, *scopes)
            ).fetchone()
            if row is None:
                return False
            if action == 'restore':
                connection.execute('UPDATE local_state SET dismissed_version = NULL WHERE entity_id = ?', (entity_id,))
                return True
            now = utc_now().isoformat()
            dismissed = row["version_hash"] if action == "dismiss" else None
            connection.execute(
                """
                INSERT INTO local_state(entity_id, seen_version, dismissed_version, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    seen_version = excluded.seen_version,
                    dismissed_version = CASE
                        WHEN excluded.dismissed_version IS NULL THEN local_state.dismissed_version
                        ELSE excluded.dismissed_version
                    END,
                    updated_at = excluded.updated_at
                """,
                (entity_id, row["version_hash"], dismissed, now),
            )
            return True
