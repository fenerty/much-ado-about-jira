from datetime import timedelta

import pytest

from models import Activity, ConnectorHealth, ConnectorResult, WorkItem, utc_now
from store import Store


def initialized_store(tmp_path):
    store = Store(tmp_path / "dashboard.sqlite3")
    store.initialize()
    return store


def test_connector_state_survives_reopen_without_sharing_mutable_objects(tmp_path):
    store = initialized_store(tmp_path)
    state = {"scope": "site/account/config", "pending": ["ENG-1", "ENG-2"], "cursor": 3}
    store.save_connector_state("jira", state)
    state["pending"].append("ENG-3")

    reopened = Store(store.database_path)
    reopened.initialize()
    loaded = reopened.load_connector_state("jira")
    assert loaded == {"scope": "site/account/config", "pending": ["ENG-1", "ENG-2"], "cursor": 3}
    loaded["pending"].clear()
    assert reopened.load_connector_state("jira")["pending"] == ["ENG-1", "ENG-2"]


@pytest.mark.parametrize("payload", ["{broken", "[]", "null", '"string"', "42"])
def test_connector_state_recovers_from_corrupt_or_non_object_json(tmp_path, payload):
    store = initialized_store(tmp_path)
    assert store.load_connector_state("jira") == {}
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?)",
            ("connector_state:jira", payload),
        )

    assert store.load_connector_state("jira") == {}
    store.save_connector_state("jira", {"scope": "reset", "cursor": 0})
    assert store.load_connector_state("jira") == {"scope": "reset", "cursor": 0}


def test_connector_states_are_independent_and_do_not_replace_baselines(tmp_path):
    store = initialized_store(tmp_path)
    with store._connect() as connection:
        connection.execute("INSERT INTO app_meta(key, value) VALUES(?, ?)", ("baseline:jira", "retained"))
    store.save_connector_state("jira", {"cursor": 1})
    store.save_connector_state("azure_devops", {"cursor": 2})
    store.save_connector_state("jira", {"cursor": 3})

    assert store.load_connector_state("jira") == {"cursor": 3}
    assert store.load_connector_state("azure_devops") == {"cursor": 2}
    with store._connect() as connection:
        assert connection.execute("SELECT value FROM app_meta WHERE key = 'baseline:jira'").fetchone()["value"] == "retained"


def test_connector_state_rejects_non_dictionary_without_overwriting_progress(tmp_path):
    store = initialized_store(tmp_path)
    store.save_connector_state("jira", {"cursor": 1})
    with pytest.raises(TypeError, match="dictionary"):
        store.save_connector_state("jira", [])
    assert store.load_connector_state("jira") == {"cursor": 1}


def snapshot_rows(store):
    with store._connect() as connection:
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY entity_id")]
            for table in ("entities", "local_state")
        }


@pytest.mark.parametrize("state", ["error", "disabled", "auth_required"])
def test_failed_health_retains_last_success_entities_and_local_triage(tmp_path, state):
    store = initialized_store(tmp_path)
    succeeded = utc_now() - timedelta(minutes=5)
    item = WorkItem(
        id="jira:issue:ENG-1", source="jira", source_type="issue", project="ENG",
        key="ENG-1", title="Investigation", url="https://example.atlassian.net/browse/ENG-1",
        status="In Progress", updated_at=succeeded, reasons=["assigned"],
    )
    activity = Activity(
        id="jira:issue:ENG-1:updated", source="jira", event_type="updated",
        item_id=item.id, item_key=item.key, item_title=item.title,
        timestamp=succeeded, summary="Issue updated", url=item.url,
    )
    store.replace_connector(ConnectorResult(
        connector="jira", work_items=[item], activities=[activity],
        health=ConnectorHealth(connector="jira", state="ok", message="Fresh",
                               last_attempt_at=succeeded, last_success_at=succeeded),
    ), 60)
    store.mark_batch([activity.id], "unread")
    store.hide_work(item.id)
    store.dismiss_updates(activity.id)
    before = snapshot_rows(store)

    attempted = utc_now()
    health = ConnectorHealth(connector="jira", state=state, message="Latest attempt",
                             last_attempt_at=attempted, error_code="latest_error",
                             coverage={"history": "unavailable"})
    store.record_health(health)

    assert snapshot_rows(store) == before
    assert health.last_success_at is None
    reopened = Store(store.database_path)
    stored_health = reopened.load()[2][0]
    assert stored_health.state == state
    assert stored_health.message == "Latest attempt"
    assert stored_health.error_code == "latest_error"
    assert stored_health.coverage == {"history": "unavailable"}
    assert stored_health.last_attempt_at == attempted
    assert stored_health.last_success_at == succeeded


def test_health_success_carryover_is_connector_scoped_and_accepts_new_success(tmp_path):
    store = initialized_store(tmp_path)
    succeeded = utc_now() - timedelta(minutes=5)
    store.record_health(ConnectorHealth(connector="jira", state="ok", message="Fresh",
                                       last_success_at=succeeded))
    store.record_health(ConnectorHealth(connector="azure_devops", state="error", message="Unavailable"))
    health = {entry.connector: entry for entry in store.load()[2]}
    assert health["azure_devops"].last_success_at is None
    assert health["jira"].last_success_at == succeeded

    newer = utc_now()
    store.record_health(ConnectorHealth(connector="jira", state="partial", message="Current work refreshed",
                                       last_success_at=newer))
    assert {entry.connector: entry for entry in store.load()[2]}["jira"].last_success_at == newer
