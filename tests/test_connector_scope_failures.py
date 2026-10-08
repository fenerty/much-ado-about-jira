import asyncio
import json
import subprocess
import threading
from dataclasses import replace

import httpx
import pytest

from config import AzureDevOpsSettings, JiraSettings, source_scope
from connectors.azure_devops import AzureDevOpsConnector
from connectors.base import CommandOutput, ConnectorFailure
from connectors.jira import JiraConnector


@pytest.fixture(params=["jira", "azure_devops"])
def connector(request, monkeypatch):
    if request.param == "jira":
        instance = JiraConnector(JiraSettings(site="https://example.atlassian.net"), 30)
    else:
        instance = AzureDevOpsConnector(AzureDevOpsSettings(organization="synthetic-org"), 30)
    monkeypatch.setattr(instance, "_find_cli", lambda: "unused")
    configure_account(instance, monkeypatch, "other@example.test")
    return instance


def configure_account(connector, monkeypatch, account):
    if connector.name == "jira":
        async def auth_status(executable, args):
            assert args == ["jira", "auth", "status"]
            return f"Email: {account}\nSite: example.atlassian.net\n"

        monkeypatch.setattr(connector, "_run", auth_status)
    else:
        def command(executable, args, timeout):
            if args[:2] == ["account", "show"]:
                return CommandOutput(json.dumps({"user": {"name": account}}), "", 0)
            assert account.strip(), "An unavailable identity must fail before token acquisition"
            return CommandOutput("synthetic-token", "", 0)

        monkeypatch.setattr("connectors.azure_devops.run_command", command)


def expected_scope(connector):
    source = connector.settings.site if connector.name == "jira" else connector.settings.organization
    return source_scope(connector.name, source, "other@example.test")


def fail_after_auth(connector, monkeypatch, error, observed, *, later_stage=False):
    async def fail(*args, **kwargs):
        assert connector.verified_cache_scope == expected_scope(connector)
        assert observed == [expected_scope(connector)]
        raise error

    if connector.name == "jira":
        if later_stage:
            async def candidates(*args, **kwargs):
                return {}, {}, []

            monkeypatch.setattr(connector, "_query_candidates", candidates)
            monkeypatch.setattr(connector, "_hydrate_candidates", fail)
        else:
            monkeypatch.setattr(connector, "_query_candidates", fail)
    else:
        if later_stage:
            async def identity(client):
                return {"id": "self"}

            monkeypatch.setattr(connector, "_connection_identity", identity)
            monkeypatch.setattr(connector, "_work_items", fail)
        else:
            monkeypatch.setattr(connector, "_connection_identity", fail)


@pytest.mark.parametrize("later_stage", [False, True])
@pytest.mark.parametrize("failure", [
    ConnectorFailure("READ_FAILED", "Synthetic read failure"),
    ConnectorFailure("TOKEN_EXPIRED", "Synthetic later auth failure", auth_required=True),
    ValueError("Synthetic payload failure"),
    OSError("Synthetic I/O failure"),
])
def test_postauth_failures_keep_verified_scope_and_notify_before_reads(
    connector, monkeypatch, failure, later_stage
):
    observed = []
    connector.scope_verified = observed.append
    fail_after_auth(connector, monkeypatch, failure, observed, later_stage=later_stage)

    result = asyncio.run(connector.refresh())

    assert result.cache_scope == expected_scope(connector)
    assert result.health.state == ("auth_required" if getattr(failure, "auth_required", False) else "error")
    assert result.health.last_success_at is None
    assert result.work_items == result.activities == []


def test_jira_notifies_verified_scope_before_loading_history(monkeypatch):
    connector = JiraConnector(JiraSettings(site="https://example.atlassian.net"), 30)
    monkeypatch.setattr(connector, "_find_cli", lambda: "unused")
    configure_account(connector, monkeypatch, "other@example.test")
    observed = []
    connector.scope_verified = observed.append

    def load(scope):
        assert observed == [scope]
        raise OSError("Synthetic checkpoint failure")

    connector.state_loader = load
    result = asyncio.run(connector.refresh())
    assert result.health.state == "error"
    assert result.cache_scope == expected_scope(connector)


def test_ado_http_failure_after_authentication_keeps_verified_scope(monkeypatch):
    connector = AzureDevOpsConnector(AzureDevOpsSettings(organization="synthetic-org"), 30)
    monkeypatch.setattr(connector, "_find_cli", lambda: "unused")
    configure_account(connector, monkeypatch, "other@example.test")
    observed = []
    connector.scope_verified = observed.append
    fail_after_auth(connector, monkeypatch, httpx.ConnectError("Synthetic connection failure"), observed)
    result = asyncio.run(connector.refresh())
    assert result.health.state == "error"
    assert result.cache_scope == expected_scope(connector)


@pytest.mark.parametrize("account,expected,code", [
    ("other@example.test", "me@example.test", "IDENTITY_MISMATCH"),
    ("", "", "IDENTITY_UNAVAILABLE"),
])
def test_unverified_identity_cannot_announce_or_attach_scope(connector, monkeypatch, account, expected, code):
    connector.settings = replace(connector.settings, expected_account=expected)
    configure_account(connector, monkeypatch, account)
    observed = []
    connector.scope_verified = observed.append

    result = asyncio.run(connector.refresh())

    assert result.health.state == "auth_required"
    assert result.health.error_code == code
    assert result.cache_scope is connector.verified_cache_scope is None
    assert observed == []


def test_new_attempt_drops_previous_verified_identity_even_if_disabled(connector, monkeypatch):
    observed = []
    connector.scope_verified = observed.append
    fail_after_auth(connector, monkeypatch, ValueError("Synthetic failure"), observed)
    first = asyncio.run(connector.refresh())
    assert first.cache_scope == expected_scope(connector)

    def unavailable_cli():
        raise OSError("Synthetic CLI failure before authentication")

    monkeypatch.setattr(connector, "_find_cli", unavailable_cli)
    second = asyncio.run(connector.refresh())
    assert second.health.state == "error"
    assert second.cache_scope is connector.verified_cache_scope is None
    assert observed == [first.cache_scope]

    connector.verified_cache_scope = first.cache_scope
    connector.settings = replace(connector.settings, enabled=False)
    disabled = asyncio.run(connector.refresh())
    assert disabled.health.state == "disabled"
    assert disabled.cache_scope is connector.verified_cache_scope is None


@pytest.mark.parametrize("failure", [RuntimeError("Synthetic unexpected failure"), asyncio.CancelledError()])
def test_unhandled_postauth_failure_leaves_verified_scope_for_coordinator(connector, monkeypatch, failure):
    observed = []
    connector.scope_verified = observed.append
    fail_after_auth(connector, monkeypatch, failure, observed)

    with pytest.raises(type(failure)):
        asyncio.run(connector.refresh())
    assert connector.verified_cache_scope == expected_scope(connector)
    assert observed == [expected_scope(connector)]


@pytest.mark.parametrize("same_account", [False, True])
def test_ado_history_memoization_is_reused_only_for_the_same_verified_account(monkeypatch, same_account):
    connector = AzureDevOpsConnector(AzureDevOpsSettings(organization="synthetic-org"), 30)
    monkeypatch.setattr(connector, "_find_cli", lambda: "unused")
    configure_account(connector, monkeypatch, "other@example.test")
    previous_account = "other@example.test" if same_account else "me@example.test"
    connector._history_cache_scope = source_scope("azure_devops", "synthetic-org", previous_account)
    cached = {("ado:workitem:1", 3): ("ado:workitem:1", ["Synthetic cached change"], "Author")}
    connector._history_cache = dict(cached)

    async def read(client):
        assert connector._history_cache_scope == expected_scope(connector)
        assert connector._history_cache == (cached if same_account else {})
        raise OSError("Synthetic later failure")

    monkeypatch.setattr(connector, "_connection_identity", read)
    result = asyncio.run(connector.refresh())
    assert result.cache_scope == expected_scope(connector)


@pytest.mark.parametrize("failure", ["empty_token", "read_error", "timeout"])
def test_ado_announces_verified_account_on_event_loop_before_token_acquisition(monkeypatch, failure):
    connector = AzureDevOpsConnector(AzureDevOpsSettings(organization="synthetic-org"), 30)
    monkeypatch.setattr(connector, "_find_cli", lambda: "unused")
    event_loop_thread = threading.get_ident()
    observed = []

    def scope_verified(scope):
        assert threading.get_ident() == event_loop_thread
        observed.append(scope)

    def command(executable, args, timeout):
        assert threading.get_ident() != event_loop_thread
        if args[:2] == ["account", "show"]:
            assert observed == []
            return CommandOutput(json.dumps({"user": {"name": "other@example.test"}}), "", 0)
        assert observed == [expected_scope(connector)]
        if failure == "read_error":
            raise OSError("Synthetic token read failure")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("synthetic-command", timeout)
        return CommandOutput("", "", 1)

    connector.scope_verified = scope_verified
    monkeypatch.setattr("connectors.azure_devops.run_command", command)
    if failure == "timeout":
        with pytest.raises(subprocess.TimeoutExpired):
            asyncio.run(connector.refresh())
    else:
        result = asyncio.run(connector.refresh())
        assert result.health.state == ("auth_required" if failure == "empty_token" else "error")
        assert result.cache_scope == expected_scope(connector)
    assert connector.verified_cache_scope == expected_scope(connector)
    assert observed == [expected_scope(connector)]


def test_jira_whitespace_only_json_email_is_unverified(monkeypatch):
    connector = JiraConnector(JiraSettings(site="https://example.atlassian.net"), 30)
    monkeypatch.setattr(connector, "_find_cli", lambda: "unused")
    observed = []
    connector.scope_verified = observed.append

    async def auth_status(executable, args):
        assert args == ["jira", "auth", "status"]
        return json.dumps({"email": " \t ", "site": "example.atlassian.net"})

    monkeypatch.setattr(connector, "_run", auth_status)
    result = asyncio.run(connector.refresh())
    assert result.health.state == "auth_required"
    assert result.health.error_code == "IDENTITY_UNAVAILABLE"
    assert result.cache_scope is connector.verified_cache_scope is None
    assert observed == []
