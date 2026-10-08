from __future__ import annotations

import os
import sys
import tomllib
import glob
import hashlib
import json
import math
from urllib.parse import urlparse
from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def _validate_integer(section: str, name: str, value: int, minimum: int = 1, maximum: int | None = None) -> None:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bounds = f"{minimum}..{maximum}" if maximum is not None else f"at least {minimum}"
        raise ValueError(f"{section}.{name} must be an integer {bounds}")


def _validate_seconds(section: str, name: str, value: int | float) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{section}.{name} must be a positive finite number")


def source_realm(connector: str, source: str) -> str:
    value = source.strip().rstrip('/').lower()
    if connector == 'jira':
        return urlparse(value if '://' in value else 'https://' + value).hostname or value
    if '://' in value:
        parsed = urlparse(value)
        if parsed.hostname == 'dev.azure.com':
            return parsed.path.strip('/').split('/')[0]
    return value


def source_scope(connector: str, source: str, account: str) -> str:
    """Opaque local namespace for a source realm and its verified account."""
    values = [connector, source_realm(connector, source), account.strip().lower()]
    return hashlib.sha256(json.dumps(values).encode('utf-8')).hexdigest()[:24]


@dataclass(frozen=True)
class AppSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    refresh_seconds: int | float = 180
    connector_timeout_seconds: int | float = 30
    activity_retention_days: int = 60
    stale_after_days: int = 30

    def __post_init__(self):
        _validate_integer('app', 'port', self.port, maximum=65535)
        for name in ('refresh_seconds', 'connector_timeout_seconds'):
            _validate_seconds('app', name, getattr(self, name))
        for name in ('activity_retention_days', 'stale_after_days'):
            _validate_integer('app', name, getattr(self, name))
        if self.host not in {'127.0.0.1', 'localhost'}:
            raise ValueError('Much ADO About Jira may only bind to localhost')


@dataclass(frozen=True)
class AzureDevOpsSettings:
    enabled: bool = True
    organization: str = ""
    expected_account: str = ""
    cli_path: str = ""
    mention_reply_days: int = 30
    max_work_items: int = 1000
    max_comment_candidates: int = 250

    def __post_init__(self):
        _validate_integer('azure_devops', 'max_work_items', self.max_work_items)
        for name in ('mention_reply_days', 'max_comment_candidates'):
            _validate_integer('azure_devops', name, getattr(self, name), minimum=0)


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
    refresh_timeout_seconds: int | float = 120
    history_timeout_seconds: int | float = 15
    history_batch_size: int = 50

    def __post_init__(self):
        for name in ('max_candidates_per_query', 'history_batch_size'):
            _validate_integer('jira', name, getattr(self, name))
        for name in ('refresh_timeout_seconds', 'history_timeout_seconds'):
            _validate_seconds('jira', name, getattr(self, name))
        for name in ('mention_reply_days', 'participation_days'):
            _validate_integer('jira', name, getattr(self, name), minimum=0)


@dataclass(frozen=True)
class Settings:
    app: AppSettings
    azure_devops: AzureDevOpsSettings
    jira: JiraSettings
    project_root: Path = PROJECT_ROOT
    state_dir: Path = field(default_factory=lambda: _default_state_dir())

    @property
    def cache_bindings(self) -> dict[str, dict]:
        bindings = {}
        for name, source, account in (
            ('azure_devops', self.azure_devops.organization, self.azure_devops.expected_account),
            ('jira', self.jira.site, self.jira.expected_account),
        ):
            realm = source_realm(name, source)
            account = account.strip().lower()
            scope = source_scope(name, realm, account)
            binding = {'realm': realm, 'account': account, 'scope': scope if account else 'unverified:' + scope,
                       'legacy_history_scope': None}
            if name == 'jira' and account:
                # The previous Jira checkpoint fingerprint proves account/site ownership.
                historical = {'site': realm, 'email': account, 'projects': self.jira.activity_projects,
                              'participation_days': self.jira.participation_days,
                              'mention_reply_days': self.jira.mention_reply_days}
                binding['legacy_history_scope'] = hashlib.sha256(json.dumps(historical, sort_keys=True).encode()).hexdigest()
            bindings[name] = binding
        return bindings

    @property
    def database_path(self) -> Path:
        return self.state_dir / "dashboard.sqlite3"


def _default_state_dir() -> Path:
    if os.environ.get("MUCH_ADO_STATE_DIR"):
        return Path(os.environ["MUCH_ADO_STATE_DIR"])
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


def user_config_path() -> Path:
    configured = os.environ.get('MUCH_ADO_CONFIG')
    if configured:
        return Path(configured)
    return (_default_state_dir() if getattr(sys, 'frozen', False) else PROJECT_ROOT) / 'settings.toml'


def load_settings(path: Path | None = None) -> Settings:
    configured = os.environ.get("MUCH_ADO_CONFIG")
    config_path = path or user_config_path()
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
    return settings
