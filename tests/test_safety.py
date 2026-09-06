import pytest

from safety import assert_ado_read_operation, assert_jira_read_command, safe_error


def test_ado_allows_get_and_wiql_query_post():
    assert_ado_read_operation("GET", "https://dev.azure.com/org/_apis/projects?api-version=7.1")
    assert_ado_read_operation("POST", "https://dev.azure.com/org/_apis/wit/wiql?api-version=7.1")


@pytest.mark.parametrize("method", ["PATCH", "PUT", "DELETE", "POST"])
def test_ado_blocks_mutations(method):
    with pytest.raises(PermissionError):
        assert_ado_read_operation(method, "https://dev.azure.com/org/_apis/wit/workitems/42")


def test_jira_command_allowlist():
    assert_jira_read_command(["jira", "auth", "status"])
    assert_jira_read_command(["jira", "workitem", "search", "--jql", "assignee=currentUser()"])
    assert_jira_read_command(["jira", "workitem", "comment", "list", "--key", "ENG-1"])
    with pytest.raises(PermissionError):
        assert_jira_read_command(["jira", "workitem", "edit", "--key", "ENG-1"])
    with pytest.raises(PermissionError):
        assert_jira_read_command(["jira", "workitem", "comment", "create", "--key", "ENG-1"])


def test_errors_redact_tokens():
    value = safe_error("Authorization: Bearer secret-token access_token='another-secret'")
    assert "secret-token" not in value
    assert "another-secret" not in value
    assert value.count("[REDACTED]") == 2
