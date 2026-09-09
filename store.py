from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from activity_details import snapshot_changes
from models import Activity, ConnectorHealth, ConnectorResult, WorkItem, utc_now


class Store:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _payload(model: WorkItem | Activity) -> tuple[str, str]:
        raw = model.model_dump(mode="json")
        raw["unread"] = False
        payload = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        version = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return payload, version

    def replace_connector(self, result: ConnectorResult, retention_days: int) -> None:
        now = utc_now().isoformat()
        baseline_key = f"baseline:{result.connector}"
        with self._lock, self._connect() as connection:
            baseline = connection.execute(
                "SELECT value FROM app_meta WHERE key = ?", (baseline_key,)
            ).fetchone()

            connection.execute(
                "UPDATE entities SET active = 0 WHERE connector = ? AND entity_kind = 'work_item'",
                (result.connector,),
            )
            previous_items = {
                row['entity_id']: json.loads(row['payload_json'])
                for row in connection.execute("SELECT entity_id, payload_json FROM entities WHERE connector = ? AND entity_kind = 'work_item'", (result.connector,))
            }
            for item in result.work_items:
                previous = previous_items.get(item.id, {})
                historical = set(previous.get('metadata', {}).get('tracked_relationships', [])) | set(previous.get('reasons', [])) | set(item.reasons)
                item.metadata['tracked_relationships'] = sorted(historical)
                if 'assigned' in historical and 'assigned' not in item.reasons and 'previously_assigned' not in item.reasons:
                    item.reasons.append('previously_assigned')
            current_items = {item.id: item.model_dump(mode='json') for item in result.work_items}
            for item in result.work_items:
                self._upsert_entity(connection, result.connector, "work_item", item, item.updated_at, now)
            for activity in result.activities:
                if activity.item_id not in previous_items and activity.item_id not in current_items:
                    parent = WorkItem(id=activity.item_id, source=activity.source, source_type='unknown', project='', key=activity.item_key, title=activity.item_title, url=activity.url, status='Unknown', updated_at=activity.timestamp, reasons=activity.reasons, metadata={'discovered_from_update': True})
                    self._upsert_entity(connection, result.connector, 'work_item', parent, parent.updated_at, now)
                    connection.execute('UPDATE entities SET active = 0 WHERE entity_id = ?', (parent.id,))
                    previous_items[parent.id] = parent.model_dump(mode='json')
                if activity.event_type == 'updated' and not activity.changes:
                    existing = connection.execute("SELECT payload_json FROM entities WHERE entity_id = ? AND entity_kind = 'activity'", (activity.id,)).fetchone()
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
                    connection, result.connector, "activity", activity, activity.timestamp, now
                )

            if baseline is None:
                rows = connection.execute(
                    "SELECT entity_id, version_hash FROM entities WHERE connector = ? AND active = 1",
                    (result.connector,),
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
            connection.execute(
                "DELETE FROM entities WHERE entity_kind = 'activity' AND event_at < ?", (cutoff,)
            )
            connection.execute(
                "DELETE FROM local_state WHERE entity_id NOT IN (SELECT entity_id FROM entities)"
            )
            self._write_health(connection, result.health)

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
                model.id,
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

    def record_health(self, health: ConnectorHealth) -> None:
        with self._lock, self._connect() as connection:
            self._write_health(connection, health)

    @staticmethod
    def _write_health(connection: sqlite3.Connection, health: ConnectorHealth) -> None:
        payload = json.dumps(health.model_dump(mode="json"), sort_keys=True)
        connection.execute(
            """
            INSERT INTO connector_runs(connector, health_json) VALUES(?, ?)
            ON CONFLICT(connector) DO UPDATE SET health_json = excluded.health_json
            """,
            (health.connector, payload),
        )

    def load(self) -> tuple[list[WorkItem], list[Activity], list[ConnectorHealth]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT e.*, s.seen_version, s.dismissed_version
                FROM entities e
                LEFT JOIN local_state s ON s.entity_id = e.entity_id
                WHERE e.active = 1 OR e.entity_kind = 'work_item'
                ORDER BY e.event_at DESC
                """
            ).fetchall()
            health_rows = connection.execute(
                "SELECT health_json FROM connector_runs ORDER BY connector"
            ).fetchall()

        work_items: list[WorkItem] = []
        activities: list[Activity] = []
        for row in rows:
            if row["dismissed_version"] == row["version_hash"]:
                continue
            data = json.loads(row["payload_json"])
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
        """Only retained, active entries hidden at their current version."""
        with self._lock, self._connect() as connection:
            rows = connection.execute("""
                SELECT e.payload_json, e.entity_kind, s.updated_at
                FROM entities e JOIN local_state s ON s.entity_id = e.entity_id
                WHERE (e.active = 1 OR e.entity_kind = 'work_item') AND e.version_hash = s.dismissed_version
                ORDER BY s.updated_at DESC
            """).fetchall()
        return [{**json.loads(row['payload_json']), 'entity_kind': row['entity_kind'],
                 'dismissed_at': row['updated_at']} for row in rows]

    def dismiss_updates(self, entity_id: str, all_for_item: bool = False) -> list[dict]:
        """Hide existing update events, never their parent item or future updates."""
        with self._lock, self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            target = connection.execute("SELECT payload_json FROM entities WHERE entity_id = ? AND entity_kind = 'activity' AND active = 1", (entity_id,)).fetchone()
            if not target:
                return []
            item_id = json.loads(target['payload_json'])['item_id']
            rows = connection.execute("""SELECT e.entity_id, e.version_hash, e.payload_json
                FROM entities e LEFT JOIN local_state s ON s.entity_id = e.entity_id
                WHERE e.entity_kind = 'activity' AND e.active = 1
                AND (s.dismissed_version IS NULL OR s.dismissed_version != e.version_hash)""").fetchall()
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
            for entry in entries:
                connection.execute("""UPDATE local_state SET dismissed_version = NULL
                    WHERE entity_id = ? AND dismissed_version = ?""",
                    (entry['entity_id'], entry['version']))

    def mark_batch(self, entity_ids: list[str], action: str) -> int:
        """Read an event plus its parent, or selected work plus its current events.

        Expand from the original selection only: reading one event must not
        recursively read its siblings. Unread remains an explicit row action.
        """
        with self._lock, self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute('SELECT entity_id, entity_kind, version_hash, payload_json FROM entities').fetchall()
            by_id = {row['entity_id']: row for row in rows}
            selected = set(entity_ids) & by_id.keys()
            targets = set(selected)
            if action == 'seen':
                selected_work = {entity_id for entity_id in selected if by_id[entity_id]['entity_kind'] == 'work_item'}
                for entity_id in selected:
                    row = by_id[entity_id]
                    if row['entity_kind'] == 'activity':
                        parent_id = json.loads(row['payload_json'])['item_id']
                        if parent_id in by_id and by_id[parent_id]['entity_kind'] == 'work_item':
                            targets.add(parent_id)
                for row in rows:
                    if row['entity_kind'] == 'activity' and json.loads(row['payload_json'])['item_id'] in selected_work:
                        targets.add(row['entity_id'])
            now = utc_now().isoformat()
            for entity_id in targets:
                connection.execute('''INSERT INTO local_state(entity_id, seen_version, updated_at)
                    VALUES(?, ?, ?) ON CONFLICT(entity_id) DO UPDATE SET
                    seen_version = excluded.seen_version, updated_at = excluded.updated_at''',
                    (entity_id, by_id[entity_id]['version_hash'] if action == 'seen' else None, now))
            return len(selected)

    def set_local_state(self, entity_id: str, action: Literal["seen", "unread", "dismiss", "restore"]) -> bool:
        if action in {'seen', 'unread'}:
            return bool(self.mark_batch([entity_id], action))
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT version_hash FROM entities WHERE entity_id = ? AND (active = 1 OR entity_kind = 'work_item')", (entity_id,)
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
