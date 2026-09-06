from datetime import timedelta

from config import JiraSettings
from connectors.azure_devops import AzureDevOpsConnector
from connectors.jira import JiraConnector
from models import utc_now


def test_jira_adf_extracts_text_and_structured_mentions():
    body = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Please review "},
                    {"type": "mention", "attrs": {"id": "abc-123", "text": "@Example Engineer"}},
                ],
            }
        ],
    }
    text, mentions = JiraConnector._adf_text_and_mentions(body)
    assert "Please review" in text
    assert "@Example Engineer" in text
    assert mentions == ["abc-123"]


def test_status_categories_are_conservative():
    assert JiraConnector._status_category("Done", "Done") == "done"
    assert JiraConnector._status_category("Pending", "In Progress") == "blocked"
    assert JiraConnector._status_category("In Progress", "In Progress") == "in_progress"
    assert JiraConnector._status_category("Ready", "To Do") == "todo"
    assert JiraConnector._status_category("Custom state", "") == "other"


def test_ado_comment_snippet_is_sanitized_and_bounded():
    result = AzureDevOpsConnector._snippet("<p>Hello &amp; welcome</p>" + "x" * 300)
    assert result.startswith("Hello & welcome")
    assert "<p>" not in result
    assert len(result) == 180


def test_jira_comment_signals_ignore_old_mentions_and_keep_recent_replies():
    connector = JiraConnector(
        JiraSettings(
            site="https://example.atlassian.net",
            mention_reply_days=30,
            participation_days=90,
        ),
        30,
    )
    identity = {"account_id": "me", "email": "me@example.test", "display_name": "Me"}
    old = utc_now() - timedelta(days=45)
    recent_self = utc_now() - timedelta(days=2)
    recent_reply = utc_now() - timedelta(days=1)
    comments = [
        {
            "id": "old",
            "created": old.isoformat(),
            "author": {"accountId": "other"},
            "body": {"type": "mention", "attrs": {"id": "me", "text": "@Me"}},
        },
        {
            "id": "self",
            "created": recent_self.isoformat(),
            "author": {"accountId": "me"},
            "body": "My note",
        },
        {
            "id": "reply",
            "created": recent_reply.isoformat(),
            "author": {"accountId": "other"},
            "body": "A response",
        },
    ]

    reasons, activity, structured = connector._comment_signals(
        "ENG-1", "Example", comments, identity
    )

    assert structured is True
    assert reasons == ["participant", "replied"]
    assert [item.event_type for item in activity] == ["reply"]
