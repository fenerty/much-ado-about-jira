# Much ADO About Jira

A local engineering work inbox for Jira, Azure DevOps, and Azure Repos. See the work that needs your attention, open it in the source system, and keep your personal triage state on your machine.

## Daily workspace

- **Priority inbox:** actionable work, review requests, unread mentions and replies, and a bounded stale-work queue.
- **My work:** active assignments across sources.
- **Pull requests:** reviews, authored changes, and discussions, with failed-check and unresolved-thread indicators where available.
- **Activity:** recent relevant updates and conversations.
- **Following:** watched, authored, and participated work.

Search the current view by title, key, or project. Filter by source or unread state; press `/` to focus search. Summary cards show whole-workspace totals and navigate to related views. Source status shows each connector's last successful sync and partial coverage. Seen and dismiss actions affect only your local inbox. Dismissed items return after a material change.

This is a personal workspace for individual engineers, not a shared team database. Each user runs their own instance with their own source permissions.

## Run locally

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

Refreshes run every three minutes by default. Each connector retains its last successful snapshot when it fails. API responses are normalized before storage; tokens and full comment bodies are not cached. Cached titles, links, and activity summaries may still contain private work information and stay local.

## Local data

SQLite lives in the runtime's user-local application-data area under `MuchADOAboutJira/dashboard.sqlite3`. Microsoft Store Python may use its package-local cache. Activity retention defaults to 60 days. The first connector snapshot is marked seen; later material changes become unread.

Keep local settings, authentication stores, databases, logs, screenshots of real work, and corporate exports out of commits. The repository includes only generic configuration and synthetic test examples. `MUCH_ADO_CONFIG` can point to a separate local TOML configuration.

## Development

FastAPI serves the API and a vanilla JavaScript/CSS interface. `connectors/` reads source data, `relevance.py` builds the inbox, `store.py` maintains SQLite snapshots and local state, and `refresh.py` coordinates updates.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests run offline with synthetic data. The browser fixture script writes only to the ignored `.runtime/browser-qa.sqlite3`, never to the normal user cache.

Local endpoints: `GET /api/dashboard`, `GET /api/health`, `POST /api/refresh`, and `POST /api/local-state` (seen/dismiss only).

## Coverage limits

Jira activity is derived from bounded issue and comment searches; its notification inbox is not mirrored. Structured mentions depend on CLI output. Azure DevOps discussion discovery starts from assigned, authored, reviewed, and locally observed work; followed subscriptions are not included. Counts describe the retrieved workspace, not every event in either system. Missing or partial source coverage is shown in connector status.
