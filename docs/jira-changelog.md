# Jira field-change history

The Jira connector can collect source-confirmed field changes through Jira Cloud's read-only changelog API. This is independent of the CLI's older-ticket discovery and comment batches. It covers discovered current tickets and retained updates, including Hidden entries; it is not an organization-wide audit trail.

## Enable after deploying the change

1. Keep normal Atlassian CLI sign-in for current work and comments. Configure `jira.expected_account` for that account.
2. Obtain a Jira API token approved for use with your site's REST API and the same account. The implementation supports email/API-token Basic authentication at the configured HTTPS site. Tokens restricted to the `api.atlassian.com` gateway need a different integration and are not supported by this transport.
3. Supply the token as `JIRA_API_TOKEN` in the process environment that launches the app. Never paste its value into settings, source files, command-line arguments, issue reports, or logs. A variable set in a terminal is inherited by processes launched from that terminal; it does not update an app that is already running or a separate Windows sign-in launcher. Use your organization's approved secret/launcher mechanism for unattended startup.
4. Set these non-secret options in the existing `[jira]` section:

   ```toml
   changelog_enabled = true
   changelog_batch_size = 8
   changelog_page_size = 100
   changelog_timeout_seconds = 10
   changelog_request_timeout_seconds = 5
   ```

5. Launch the deployed app with that environment. Under **Sync details**, check Jira field-history progress. Capture verifies that REST `/myself` identifies the same account as the signed-in CLI before reading any changelogs. A token for another account is rejected even if it can read the same tickets.

Existing settings default to capture off. Enabling capture without a usable token or matching identity reports incomplete change-history coverage while preserving successful current-work reads. The app does not start an interactive login loop or extract credentials from the CLI's private authentication store.

The token remains in memory. Checkpoints contain sanitized progress and error codes, not tokens, authorization headers, or raw HTTP bodies. Requests use the configured HTTPS site, do not follow redirects, and build page URLs locally rather than trusting response-supplied links.

## Progress and retained triage

Pages advance in bounded rotating batches. The phase budget and each whole GET request have separate configurable limits; reaching the phase boundary defers work rather than recording a source-read failure. Saved progress is scoped to the verified source/account and commits in the same transaction as accepted events. A failed page or cache write remains replayable. Increasing activity retention revisits older pages; it cannot recover history the source no longer exposes.

Source changelog IDs identify events independently of page positions. An exact timestamp can link a previously generic update to one source changelog event. Enrichment preserves the retained entry's ID, read/unread acknowledgement, hidden state, and pending Undo action. Different source events remain distinct, including events sharing a timestamp; an additional historical event is not assumed read solely because of its age.

Field changes use source-confirmed before/after values for supported scalar fields. Rich text, bodies, identities, and complex values are summarized safely instead of caching their raw contents. Source history and **Between local refreshes** remain distinct. A source change without a trustworthy time cannot be assigned a fabricated date.

## Interpreting gaps

- **Capture off / changed fields were not captured:** existing snapshots can identify only status, assignee, priority, and title differences between local refreshes. Older-ticket batches do not reconstruct intermediate changes.
- **Details pending:** configured history collection has not reached that page or ticket yet.
- **Read failed:** a bounded source request failed, a particular ticket became deleted/inaccessible, or a page contained invalid source records; saved progress remains available for retry without resetting triage.
- **History unavailable:** setup, account verification, global REST authorization, or unmatched source timestamps prevent capture. Sync details separates these gaps from ordinary progress.

Open the ticket's History view for information not represented in the local inbox. Clearing the cache or resetting read state is not a recovery step.

## API references

- [Jira Cloud changelog GET and permissions](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-changelog-get)
- [Atlassian API-token Basic authentication](https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/)

These instructions describe configuration after deployment. Opening or reviewing a PR does not update the running app.
