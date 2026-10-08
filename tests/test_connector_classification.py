import asyncio
import json
from datetime import timedelta

import pytest

from config import AppSettings, AzureDevOpsSettings, JiraSettings, Settings, source_scope
from connectors.azure_devops import AzureDevOpsConnector
from connectors.base import CommandOutput
from connectors.jira import JiraConnector
from models import ConnectorHealth, ConnectorResult, utc_now
from refresh import RefreshCoordinator
from store import Store


@pytest.mark.parametrize(
    "status,source_category,expected",
    [
        ("Unresolved", "To Do", "todo"),
        ("Incomplete", "In Progress", "in_progress"),
        ("Ready for Review", "To Do", "todo"),
        ("Closed", "In Progress", "in_progress"),
        ("Pending", "Done", "done"),
        ("Pending", "In Progress", "blocked"),
    ],
)
def test_jira_normalization_respects_source_category(status, source_category, expected):
    connector = JiraConnector(JiraSettings(site="https://example.atlassian.net"), 30)
    record = {
        "key": "ENG-1",
        "fields": {
            "summary": "Synthetic status",
            "status": {"name": status, "statusCategory": {"name": source_category}},
            "updated": utc_now().isoformat(),
        },
    }
    items, _, _ = asyncio.run(connector._normalize(
        "unused", {"ENG-1": record}, {"ENG-1": {"assigned"}}, {}
    ))
    assert items[0].status == status
    assert items[0].status_category == expected


@pytest.mark.parametrize(
    "status,expected",
    [
        ("Unresolved", "other"),
        ("Incomplete", "other"),
        ("Not completed", "other"),
        ("Resolved", "done"),
        ("Completed", "done"),
        ("In Review", "in_progress"),
        ("Ready", "todo"),
    ],
)
def test_jira_category_fallback_uses_precise_labels(status, expected):
    assert JiraConnector._status_category(status, "") == expected


@pytest.mark.parametrize(
    "identity,author",
    [
        (
            {"account_id": "self", "email": "me@example.test", "display_name": "Alex Smith"},
            {"accountId": "other", "emailAddress": "me@example.test", "displayName": "Alex Smith"},
        ),
        (
            {"email": "me@example.test", "display_name": "Alex Smith"},
            {"emailAddress": "other@example.test", "displayName": "Alex Smith"},
        ),
    ],
)
def test_jira_identifier_conflicts_cannot_create_participation_or_reply(identity, author):
    connector = JiraConnector(JiraSettings(site="https://example.atlassian.net"), 30)
    comments = [
        {
            "id": "other-person",
            "created": (utc_now() - timedelta(days=2)).isoformat(),
            "author": author,
            "body": "Another person's comment",
        },
        {
            "id": "later-comment",
            "created": (utc_now() - timedelta(days=1)).isoformat(),
            "author": {"accountId": "third", "emailAddress": "third@example.test"},
            "body": "A later comment",
        },
    ]
    reasons, activities, _ = connector._comment_signals("ENG-1", "Synthetic", comments, identity)
    assert reasons == []
    assert activities == []
    assert not JiraConnector._is_self(author, identity)


def test_jira_matching_account_id_survives_email_or_name_change():
    assert JiraConnector._is_self(
        {"accountId": "SELF", "emailAddress": "new@example.test", "displayName": "New Name"},
        {"account_id": "self", "email": "old@example.test", "display_name": "Old Name"},
    )


def test_jira_email_and_display_fallbacks_require_no_comparable_account_ids():
    assert JiraConnector._is_self(
        {"emailAddress": "ME@example.test", "displayName": "New Name"},
        {"account_id": "self", "email": "me@example.test", "display_name": "Old Name"},
    )
    assert JiraConnector._is_self(
        {"accountId": "self", "displayName": "Alex Smith"},
        {"email": "me@example.test", "display_name": "Alex Smith"},
    )
    assert not JiraConnector._is_self({}, {})


@pytest.mark.parametrize("self_vote", [None, 0, 10])
def test_ado_author_attention_includes_other_reviewers_negative_vote(monkeypatch, self_vote):
    connector = AzureDevOpsConnector(AzureDevOpsSettings(organization="synthetic-org"), 30)
    reviewers = [{"id": "other", "displayName": "Other Reviewer", "vote": -5}]
    roles = {"author"}
    if self_vote is not None:
        reviewers.append({"id": "self", "displayName": "Self", "vote": self_vote})
        roles.add("reviewer")
    pr = {
        "pullRequestId": 7,
        "repository": {"id": "repo", "name": "repo", "project": {"name": "ENG"}},
        "createdBy": {"id": "self"},
        "reviewers": reviewers,
        "creationDate": utc_now().isoformat(),
    }

    async def read(client, method, url, **kwargs):
        assert method == "GET"
        return {"value": []}, {}

    monkeypatch.setattr(connector, "_json", read)
    item, activities = asyncio.run(connector._normalize_pr(None, pr, roles, "self"))
    assert item.actionable
    assert "requested_changes" in item.reasons
    assert item.metadata["reviewer_vote"] == self_vote
    assert activities == []


def test_ado_reviewer_vote_stays_local_to_self_when_another_reviewer_objects(monkeypatch):
    connector = AzureDevOpsConnector(AzureDevOpsSettings(organization="synthetic-org"), 30)
    pr = {
        "pullRequestId": 7,
        "repository": {"id": "repo", "name": "repo", "project": {"name": "ENG"}},
        "reviewers": [{"id": "self", "vote": 10}, {"id": "other", "vote": -5}],
    }

    async def read(client, method, url, **kwargs):
        return {"value": []}, {}

    monkeypatch.setattr(connector, "_json", read)
    item, _ = asyncio.run(connector._normalize_pr(None, pr, {"reviewer"}, "self"))
    assert not item.actionable
    assert "requested_changes" not in item.reasons
    assert item.metadata["reviewer_vote"] == 10


def test_ado_refresh_scopes_cache_to_authenticated_account(monkeypatch):
    connector = AzureDevOpsConnector(
        AzureDevOpsSettings(organization="synthetic-org", expected_account="me@example.test"), 30
    )
    monkeypatch.setattr(connector, "_find_cli", lambda: "unused")

    def command(executable, args, timeout):
        if args[:2] == ["account", "show"]:
            return CommandOutput(json.dumps({"user": {"name": "me@example.test"}}), "", 0)
        return CommandOutput("synthetic-token", "", 0)

    async def identity(client):
        return {"id": "self", "providerDisplayName": "Display Name"}

    async def work(client, identity):
        connector._work_partial = False
        return [], [], []

    async def pull_requests(client, identity):
        return [], [], False

    monkeypatch.setattr("connectors.azure_devops.run_command", command)
    monkeypatch.setattr(connector, "_connection_identity", identity)
    monkeypatch.setattr(connector, "_work_items", work)
    monkeypatch.setattr(connector, "_pull_requests", pull_requests)
    result = asyncio.run(connector.refresh())
    assert result.health.state == "ok"
    assert result.cache_scope == source_scope("azure_devops", "synthetic-org", "me@example.test")


def test_jira_refresh_scopes_cache_to_verified_account_and_site(monkeypatch):
    connector = JiraConnector(
        JiraSettings(site="https://example.atlassian.net", expected_account="me@example.test"), 30
    )
    monkeypatch.setattr(connector, "_find_cli", lambda: "unused")

    async def auth_status(executable, args):
        assert args == ["jira", "auth", "status"]
        return "Email: me@example.test\nSite: example.atlassian.net\n"

    async def search(executable, jql, *, limit=None):
        return []

    monkeypatch.setattr(connector, "_run", auth_status)
    monkeypatch.setattr(connector, "_search", search)
    result = asyncio.run(connector.refresh())
    assert result.health.state == "partial"
    assert result.cache_scope == source_scope("jira", "example.atlassian.net", "me@example.test")


def test_jira_loads_verified_history_before_discovery(monkeypatch):
    connector = JiraConnector(JiraSettings(site="https://example.atlassian.net"), 30)
    identity = {"site": "example.atlassian.net", "email": "me@example.test"}
    connector._ensure_history(identity)
    connector._history.cursors["assigned"] = "ENG-99"
    saved = connector.checkpoint()
    connector.load_state({})
    requested_scopes = []
    queries = []

    def loader(scope):
        requested_scopes.append(scope)
        return saved

    async def auth_status(executable, args):
        assert args == ["jira", "auth", "status"]
        return "Email: me@example.test\nSite: example.atlassian.net\n"

    async def search(executable, jql, *, limit=None):
        assert connector._saved_state == saved
        queries.append(jql)
        return []

    connector.state_loader = loader
    monkeypatch.setattr(connector, "_find_cli", lambda: "unused")
    monkeypatch.setattr(connector, "_run", auth_status)
    monkeypatch.setattr(connector, "_search", search)
    result = asyncio.run(connector.refresh())
    assert requested_scopes == [source_scope("jira", identity["site"], identity["email"])]
    assert any('key > "ENG-99"' in query for query in queries)
    assert result.cache_scope == requested_scopes[0]


def test_jira_failed_authentication_cannot_load_verified_history(monkeypatch):
    connector = JiraConnector(
        JiraSettings(site="https://example.atlassian.net", expected_account="me@example.test"), 30
    )
    requested_scopes = []
    connector.state_loader = lambda scope: requested_scopes.append(scope)

    async def auth_status(executable, args):
        return "Email: other@example.test\nSite: example.atlassian.net\n"

    monkeypatch.setattr(connector, "_find_cli", lambda: "unused")
    monkeypatch.setattr(connector, "_run", auth_status)
    result = asyncio.run(connector.refresh())
    assert result.health.state == "auth_required"
    assert result.cache_scope is None
    assert requested_scopes == []


def test_jira_unspecified_account_restores_verified_checkpoint_after_restart(tmp_path, monkeypatch):
    settings = Settings(
        AppSettings(), AzureDevOpsSettings(enabled=False),
        JiraSettings(site="https://example.atlassian.net"), state_dir=tmp_path,
    )
    verified_scope = source_scope("jira", "example.atlassian.net", "me@example.test")
    first = Store(settings.database_path, cache_bindings=settings.cache_bindings)
    first.initialize()
    first.replace_connector(ConnectorResult(
        connector="jira", cache_scope=verified_scope,
        health=ConnectorHealth(connector="jira", state="ok", message="Synthetic first refresh"),
    ), 60)
    history = JiraConnector(settings.jira, 30)
    history._ensure_history({"site": "example.atlassian.net", "email": "me@example.test"})
    history._history.cursors["assigned"] = "ENG-99"
    history._history.records["ENG-98"] = {"assigned"}
    first.save_connector_state("jira", history.checkpoint())
    reopened = Store(settings.database_path, cache_bindings=settings.cache_bindings)
    reopened.initialize()
    assert reopened.load_connector_state("jira") == {}
    coordinator = RefreshCoordinator(settings, reopened)
    connector = coordinator.connectors[1]
    queries = []

    async def auth_status(executable, args):
        assert args == ["jira", "auth", "status"]
        return "Email: me@example.test\nSite: example.atlassian.net\n"

    async def search(executable, jql, *, limit=None):
        queries.append(jql)
        return []

    async def view(executable, key):
        assert key == "ENG-98"
        return {"key": key, "fields": {
            "summary": "Synthetic saved history",
            "status": {"name": "Done", "statusCategory": {"name": "Done"}},
            "assignee": {"emailAddress": "me@example.test"},
            "updated": utc_now().isoformat(),
        }}

    monkeypatch.setattr(connector, "_find_cli", lambda: "unused")
    monkeypatch.setattr(connector, "_run", auth_status)
    monkeypatch.setattr(connector, "_search", search)
    monkeypatch.setattr(connector, "_view", view)
    asyncio.run(coordinator.refresh())
    checkpoint = reopened.load_connector_state("jira", cache_scope=verified_scope)
    assert any('key > "ENG-99"' in query for query in queries)
    assert checkpoint["records"] == {"ENG-98": ["assigned"]}
    assert checkpoint["passes"]["assigned"] == 1
