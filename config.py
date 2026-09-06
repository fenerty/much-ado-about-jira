from __future__ import annotations

import os
import tomllib
import glob
from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class AppSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    refresh_seconds: int = 180
    connector_timeout_seconds: int = 30
    activity_retention_days: int = 60
    stale_after_days: int = 30


@dataclass(frozen=True)
class AzureDevOpsSettings:
    enabled: bool = True
    organization: str = ""
    expected_account: str = ""
    cli_path: str = ""
    mention_reply_days: int = 30
    max_work_items: int = 1000
    max_comment_candidates: int = 250


@dataclass(frozen=True)
class JiraSettings:
    enabled: bool = True
    site: str = ""
    expected_account: str = ""
    cli_path: str = ""
    activity_projects: tuple[str, ...] = field(default_factory=tuple)
    mention_reply_days: int = 30
    participation_days: int = 90
    max_candidates_per_query: int = 1000


@dataclass(frozen=True)
class Settings:
    app: AppSettings
    azure_devops: AzureDevOpsSettings
    jira: JiraSettings
    project_root: Path = PROJECT_ROOT
    state_dir: Path = field(default_factory=lambda: _default_state_dir())

    @property
    def database_path(self) -> Path:
        return self.state_dir / "dashboard.sqlite3"


def _default_state_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    package_local = glob.glob(
        str(base / "Packages" / "PythonSoftwareFoundation.Python.3.13_*" / "LocalCache" / "Local")
    )
    if package_local:
        base = Path(package_local[0])
    return base / "MuchADOAboutJira"


def _section(data: dict, name: str) -> dict:
    value = data.get(name, {})
    return value if isinstance(value, dict) else {}


def load_settings(path: Path | None = None) -> Settings:
    configured = os.environ.get("MUCH_ADO_CONFIG")
    config_path = path or (Path(configured) if configured else PROJECT_ROOT / "settings.toml")
    if not config_path.exists() and not path and not configured:
        config_path = PROJECT_ROOT / "settings.example.toml"
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    app_raw = _section(raw, "app")
    ado_raw = _section(raw, "azure_devops")
    jira_raw = _section(raw, "jira")
    jira_raw["activity_projects"] = tuple(jira_raw.get("activity_projects", ()))

    settings = Settings(
        app=AppSettings(**app_raw),
        azure_devops=AzureDevOpsSettings(**ado_raw),
        jira=JiraSettings(**jira_raw),
    )
    if settings.app.host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Much ADO About Jira may only bind to localhost")
    return settings
