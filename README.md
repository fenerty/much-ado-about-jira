# Much ADO About Jira

A local engineering work inbox for Jira, Azure DevOps, and Azure Repos. See the work that needs your attention, open it in the source system, and keep your personal triage state on your machine.

## Daily workspace

There are two work views:

- **Updates:** catch up on individual changes, comments, mentions, and replies. Unread only is initially enabled; turn it off to see retained updates already marked seen.
- **My work:** one row per current or previous assignment, pull request, or followed item, grouped by status by default, completed last, with recent changes first within each group. Filter by relationship when planning work or reviewing code. Hiding an update never removes its work item.

In Updates, choose **All updates** (the default) or **Latest per item**. Latest per item shows the newest update for each item that matches your current filters, before pagination. The choice is saved in this browser. All captured updates remain available when you switch back. Mark read still affects just that update, so an older unread update may appear next; **Read all for item** keeps its existing broader scope. Bulk selection includes only updates represented by the chosen mode, across all matching pages. Item counts in Latest per item are separate from the total update counts.

The **Hidden** utility restores retained hidden entries, including work items hidden from My work. Hide offers an explicit choice: only this update, or all current updates for its item. It never mutes future updates or writes to source systems. An immediate Undo action restores that dismissal; Hidden remains available after reloading. New dismissals preserve unread state. Older dismissals may have already marked entries seen, so restored updates may require turning off Unread only. Activity that expired under the configured retention policy cannot be restored.

Use the **Dark mode** toggle for a low-glare graphite theme. The app follows your system theme until you choose; your choice is saved in this browser. Search by title, key, or project, filter by source, or press `/` to focus search. Each view remembers its filters, shown-row count, and selections across navigation and reload. **About Updates/My work** opens a concise, view-specific guide with optional discovery details. Source status shows freshness and partial coverage.

This is a personal workspace for individual engineers, not a shared team database. Each user runs their own instance with their own source permissions.

## Download and launch (Windows)

Download the Windows ZIP from [Releases](https://github.com/fenerty/much-ado-about-jira/releases), extract the entire folder, and double-click **MuchADOAboutJira.exe**. No GitHub account, fork, Git, or Python installation is required.

The first-run form asks for your work email and Jira site and/or Azure organization. It downloads the official source tools and opens interactive sign-in. Leave unused sources blank. Your organization may require approval for these tools or account access. This initial preview is unsigned; follow your organization's software policy if Windows shows an unknown-publisher warning. See the included **READ ME FIRST.txt** for updates and troubleshooting.

The application runs locally, starts quietly at Windows sign-in by default, and opens in your default browser when launched manually. Sign-in and source permissions are still user-controlled. The source tools are downloaded separately, keeping the dashboard download smaller.

## Run from source (developers)

Requires Windows and Python 3.13.

```powershell
.\setup.ps1
Copy-Item settings.example.toml settings.toml
```

Edit `settings.toml` with your organization, Jira site, activity projects, and expected account. Enable only the connectors you want. This file is ignored by Git. Without it, the app uses the example settings with both connectors disabled.

```powershell
.\setup-connectors.ps1
```

The connector installer uses Azure CLI from `PATH` when available, otherwise downloads it into the ignored `.tools` directory. It also downloads the official Atlassian CLI. Sign in interactively using your own account:

```powershell
# Use az from PATH if already installed; otherwise:
& .\.tools\azure-cli\bin\az.cmd login
& .\.tools\acli.exe jira auth login --web
.\run.ps1
```

The dashboard opens at [localhost:8765](http://127.0.0.1:8765). Both connectors check the configured expected identity before querying work. The server only binds to localhost.

## Read-only connectors

Azure DevOps uses the authenticated Azure CLI session to obtain a short-lived delegated token in memory. The HTTP guard permits GET and read-query WIQL POST requests. Jira uses the Atlassian CLI OAuth session with an allowlist of authentication status, issue search/view, and comment-list commands. The app does not create credentials or write to either source system.

Refreshes run every three minutes by default. Each connector retains the verified account's last successful snapshot when it fails. API responses are normalized before storage; tokens and full comment bodies are not cached. Cached titles, links, and activity summaries may still contain private work information and stay local.

## Local data

SQLite lives in the runtime's user-local application-data area under `MuchADOAboutJira/dashboard.sqlite3`. Microsoft Store Python may use its package-local cache. Activity retention defaults to 60 days. The first connector snapshot is marked seen; later material changes become unread.

Cached work, history progress, and personal read/hidden state are separated by source site or organization and account. Changing sources or accounts does not carry another account’s relationships or hidden entries into the new workspace; returning to the original configuration restores its retained state. When no expected account is configured, saved work is shown only after the connector verifies the current identity. Verification switches the visible workspace immediately, even if the following sync fails; an account without a saved snapshot shows an empty workspace with the sync error.

On upgrade, a single original Jira snapshot can be carried forward when its saved history fingerprint proves the same source and account. Older Jira caches may already mix sources or accounts, and legacy ADO cache labels do not prove account identity. Their ownership cannot be assigned safely: they stay in the local database, separate from the new workspace, whose first successful snapshot establishes a fresh baseline. Keep a backup of the database before upgrades if you need to retain older triage state.

If you can confirm that a connector's legacy cache belongs entirely to the currently configured source and account, `scripts/restore-legacy-read-state.py` can preserve update read/unread and hidden state without importing legacy history into the visible workspace. Preview with `python scripts/restore-legacy-read-state.py --config <settings.toml> --database <dashboard.sqlite3> --connector jira --scope <verified-scope>`. Add `--apply --backup <new-backup.sqlite3>` only after confirming ownership. A successful account-scoped refresh and an explicitly configured account are required; the backup must be a new file.

The binding applies to updates already rediscovered and to later historical batches. Only identical event content can inherit a read or hidden acknowledgement; new or changed updates remain unread. Explicit actions in the new workspace take precedence over imported state. The initial automatic baseline is replaced by the confirmed legacy state, and the binding cannot be reused for another source or account. No Jira or ADO data is modified. Update age alone never proves that an update has been read.

Keep local settings, authentication stores, databases, logs, screenshots of real work, and corporate exports out of commits. The repository includes only generic configuration and synthetic test examples. `MUCH_ADO_CONFIG` can point to a separate local TOML configuration.

## Development

FastAPI serves the API and a vanilla JavaScript/CSS interface. `connectors/` reads source data, `relevance.py` builds the inbox, `store.py` maintains SQLite snapshots and local state, and `refresh.py` coordinates updates.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests run offline with synthetic data. The browser fixture script writes only to the ignored `.runtime/browser-qa.sqlite3`, never to the normal user cache.

Local endpoints: `GET /api/dashboard`, `GET /api/health`, `POST /api/refresh`, and `POST /api/local-state` (seen, update dismissal, and restore), and `POST /api/undo-dismiss` (version-scoped restoration).

## Coverage limits

Jira activity is derived from bounded issue and comment searches; its notification inbox is not mirrored. Structured mentions depend on CLI output. Azure DevOps discussion discovery starts from assigned, authored, reviewed, and locally observed work; followed subscriptions are not included. Counts describe the retrieved workspace, not every event in either system. Missing or partial source coverage is shown in connector status.

Activity details distinguish **ADO revision history** (the latest revision for up to 50 recent retrieved work items) from **Between local refreshes** (observed status, assignee, priority, or title differences). The current Jira CLI does not expose changelog retrieval. Local comparisons cannot reconstruct intermediate changes or identify their actors. Existing events without details say so; open the source item for its full History view.

The overview distinguishes all open PRs from PRs needing your review (reviewer, no vote, non-draft). **Status first** is the default sort; **Recently updated** and **Needs attention first** are optional. Prior assignment uses source history queries, while historical comment/subscription coverage remains incomplete. See [PRODUCT.md](PRODUCT.md) for the interaction contract and remaining coverage work.

### Start automatically on Windows

Settings → Launch at Windows sign-in is enabled on first launch. It registers a startup entry for the current Windows user and runs the local server through `pythonw.exe --background`, without a console or browser window. Turn it off in Settings to stop automatic launches; the running app remains available for the current session. Startup needs the checkout and its `.venv` to stay at their installed paths. Synchronization runs while the app is running and the computer is awake; source sign-in requirements still apply.


## Display, selection, and coverage

Settings saves 30/50/100/250 rows per page or Show all by default in your browser. Select all matching rows includes undisplayed pages and collapsed groups, with an explicit selected count. Read/unread controls belong to Updates only. My work is an inventory with plain counts and no read-state filtering or styling. Hide on a work item keeps it hidden until restored, without hiding or reading its updates. Reading one update does not read its sibling updates. Mark unread affects only explicitly selected rows. These actions preserve hidden state and never mark future changes read. Status groups can be collapsed and reordered with accessible arrow controls; these preferences persist per browser.

My work has no age cutoff after discovery. Updates retains captured events for 60 days by default, not every historical source event. The UI reports distinct parent-item counts and the configured discovery windows/caps beside the view. Consequently My work can have more entries than Updates.

Jira refreshes current work before collecting older history. Active searches retain their configured per-query cap. Completed assignments, previous assignments, watched items, and authored items are discovered through one bounded page per refresh, rotating between those four relationships. Keyset cursors eventually traverse all-time history without a date cutoff; completing a pass restarts that relationship so older tickets with changed status or relationships can be rediscovered. Discovery cursors and a minimal key/relationship inventory are saved locally and scoped to the verified Jira account, site, and discovery settings.

Discovered history is checked in batches of 16. Successful checks and the next batch position survive app restarts. Failed keys remain retryable without blocking later tickets. Progress distinguishes checks over the discovered inventory from source discovery, whose remaining ticket count is unknown. The ETA covers only already-discovered tickets, and is not proof of all-time coverage. Cached historical relationships do not turn reopened, reassigned tickets into current assignments.

Jira has an independent whole-refresh budget (`jira.refresh_timeout_seconds`, default 120), while individual commands use `app.connector_timeout_seconds` (default 30). Historical discovery and hydration each have a separate phase limit (`jira.history_timeout_seconds`, default 15); discovery pages default to 50 (`jira.history_batch_size`). A slow optional history or comment read produces partial coverage while retaining successful current-work reads. Timed-out or canceled Jira CLI processes and bridge children are terminated. Error status retains the last successful-sync time and the cached snapshot. Existing settings files receive the new Jira defaults automatically; no local timeout change is required.

## Build and contribution workflow

Use a feature branch and pull request; keep company data and local settings out of commits. Windows CI runs the offline tests and builds a downloadable ZIP artifact for each PR. Maintainers publish tested artifacts as GitHub releases; no release is auto-promoted or merged.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build-windows.ps1
.\scripts\smoke-windows.ps1
```

The package explicitly includes only application assets and generic setup files. Builds use [PyInstaller's one-folder Windows packaging](https://pyinstaller.org/en/stable/usage.html). Generated packages, tool downloads, databases, and personal configuration are ignored by Git. Release artifacts include SHA-256 checksums. CI also launches the windowed executable against an isolated configuration with both sources disabled and verifies its dashboard. A clean-machine first-run sign-in is a separate validation step from offline tests and package launch tests.

Unread rows use a distinct background, accent edge, and label. No recent changes means the last retrieved source update exceeds the configured aging threshold (30 days by default), not overdue work or a sync failure. Routine history progress is available in Sync details.

**Date & activity** filters each view by All dates or Within the last X days, weeks, months, or years, plus Recent activity only (within the configured activity window, 30 days by default). Updates use event time; My work uses the last source update. Calendar months/years clamp to the last valid day. Recent activity uses the related work item’s last source update and excludes older, missing-date, or unverified items. Recently updated completed work is included. Both filters combine, persist per view, and only narrow collected data; they do not extend retention or discovery windows.

Updates also offers **Read all for item** when other unread updates exist for that item. It marks all currently retained related updates read, including filtered or hidden updates, without unhiding anything. Future updates remain unread. Ordinary Mark read keeps its single-update scope. Hide moves updates out of the inbox without marking them read; Hidden and Undo restore retained entries.
