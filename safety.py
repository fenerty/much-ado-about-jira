from __future__ import annotations

import re
from urllib.parse import urlparse


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)(access[_ -]?token[\"'=:\s]+)[A-Za-z0-9._~+\-/=]+"),
    re.compile(r"(?i)(refresh[_ -]?token[\"'=:\s]+)[A-Za-z0-9._~+\-/=]+"),
)


def safe_error(value: object, limit: int = 280) -> str:
    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text[:limit]


def assert_ado_read_operation(method: str, url: str) -> None:
    normalized = method.upper()
    if normalized == "GET":
        return
    path = urlparse(url).path.lower().rstrip("/")
    if normalized == "POST" and path.endswith("/_apis/wit/wiql"):
        return
    raise PermissionError(f"Corporate write blocked: {normalized} {path}")


def assert_jira_read_command(args: list[str]) -> None:
    lowered = [part.lower() for part in args]
    allowed_prefixes = (
        ["jira", "auth", "status"],
        ["jira", "workitem", "search"],
        ["jira", "workitem", "view"],
        ["jira", "workitem", "comment", "list"],
    )
    if any(lowered[: len(prefix)] == prefix for prefix in allowed_prefixes):
        return
    raise PermissionError(f"Corporate Jira write command blocked: {' '.join(args[:4])}")
