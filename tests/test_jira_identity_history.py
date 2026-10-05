import asyncio

import pytest

from config import JiraSettings
from connectors.base import ConnectorFailure
from connectors.jira import JiraConnector
from models import utc_now


@pytest.mark.parametrize("expose_assignee_email", [False, True])
def test_text_identity_without_current_assignments_is_not_inferred_from_saved_history(
    monkeypatch, expose_assignee_email
):
    connector = JiraConnector(
        JiraSettings(
            site="https://example.atlassian.net",
            expected_account="me@example.test",
        ),
        30,
    )
    identity_scope = {"site": "example.atlassian.net", "email": "me@example.test"}
    connector._ensure_history(identity_scope)
    connector._history.records["ENG-1"] = {"assigned"}
    connector.load_state(connector.checkpoint())
    captured_identity = {}
    authenticate = connector._authenticate
    searches = []
    assignee = {"accountId": "other-account", "displayName": "Other Person"}
    if expose_assignee_email:
        assignee["emailAddress"] = "other@example.test"
    reopened = {
        "key": "ENG-1",
        "fields": {
            "summary": "Synthetic reopened and reassigned ticket",
            "status": {
                "name": "In Progress",
                "statusCategory": {"name": "In Progress"},
            },
            "assignee": assignee,
            "updated": utc_now().isoformat(),
            "comment": {
                "comments": [{
                    "id": "synthetic-comment",
                    "created": utc_now().isoformat(),
                    "author": dict(assignee),
                    "body": {
                        "type": "mention",
                        "attrs": {"id": "other-account", "text": "@Other Person"},
                    },
                }],
                "total": 1,
            },
        },
    }

    async def run(executable, args):
        assert args == ["jira", "auth", "status"]
        return "Email: me@example.test\nSite: example.atlassian.net\n"

    async def capture_authenticate(executable):
        identity = await authenticate(executable)
        captured_identity["value"] = identity
        captured_identity["original"] = dict(identity)
        return identity

    async def search(executable, jql, *, limit=None):
        searches.append(jql)
        return []

    async def view(executable, key):
        assert key == "ENG-1"
        return reopened

    monkeypatch.setattr(connector, "_find_cli", lambda: "synthetic-acli")
    monkeypatch.setattr(connector, "_run", run)
    monkeypatch.setattr(connector, "_authenticate", capture_authenticate)
    monkeypatch.setattr(connector, "_search", search)
    monkeypatch.setattr(connector, "_view", view)

    result = asyncio.run(connector.refresh())

    assert any(query.startswith("assignee = currentUser()") for query in searches)
    assert connector._fresh_roles == {}
    assert captured_identity["value"] == captured_identity["original"]
    assert captured_identity["value"]["account_id"] == ""
    assert captured_identity["value"]["email"] == "me@example.test"
    assert len(result.work_items) == 1
    assert result.work_items[0].status_category == "in_progress"
    assert result.work_items[0].reasons == ["previously_assigned"]
    assert result.work_items[0].metadata["tracked_relationships"] == ["assigned"]
    assert {activity.event_type for activity in result.activities} == {"updated"}


@pytest.mark.parametrize("actual_email", ["other@example.test", "notme@example.test"])
def test_authentication_compares_the_parsed_email_exactly(monkeypatch, actual_email):
    connector = JiraConnector(
        JiraSettings(site="https://example.atlassian.net", expected_account="me@example.test"),
        30,
    )

    async def run(executable, args):
        return (
            f"Email: {actual_email}\nSite: example.atlassian.net\n"
            "Note: expected account is me@example.test\n"
        )

    monkeypatch.setattr(connector, "_run", run)
    with pytest.raises(ConnectorFailure) as failure:
        asyncio.run(connector._authenticate("synthetic-acli"))
    assert failure.value.code == "IDENTITY_MISMATCH"
    assert failure.value.auth_required


def test_authentication_matches_email_case_insensitively(monkeypatch):
    connector = JiraConnector(
        JiraSettings(site="https://example.atlassian.net", expected_account=" Me@Example.Test "),
        30,
    )

    async def run(executable, args):
        return "Email: ME@EXAMPLE.TEST\nSite: example.atlassian.net\n"

    monkeypatch.setattr(connector, "_run", run)
    identity = asyncio.run(connector._authenticate("synthetic-acli"))
    assert identity["email"] == "me@example.test"


def test_authentication_does_not_substitute_expected_email_for_missing_account(monkeypatch):
    connector = JiraConnector(
        JiraSettings(site="https://example.atlassian.net", expected_account="me@example.test"),
        30,
    )

    async def run(executable, args):
        return "Site: example.atlassian.net\nNote: expected account is me@example.test\n"

    monkeypatch.setattr(connector, "_run", run)
    with pytest.raises(ConnectorFailure) as failure:
        asyncio.run(connector._authenticate("synthetic-acli"))
    assert failure.value.code == "IDENTITY_UNAVAILABLE"
    assert failure.value.auth_required
