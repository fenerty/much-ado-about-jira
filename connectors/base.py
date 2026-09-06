from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from pathlib import Path
import shutil


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ConnectorFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, auth_required: bool = False):
        super().__init__(message)
        self.code = code
        self.auth_required = auth_required


@dataclass(frozen=True)
class CommandOutput:
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class CommandSpec:
    prefix: list[str]
    json_arguments: bool = False


def run_command(executable: str | CommandSpec, args: list[str], timeout: int) -> CommandOutput:
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    if isinstance(executable, CommandSpec):
        command = [*executable.prefix, json.dumps(args)] if executable.json_arguments else [*executable.prefix, *args]
    else:
        command = [executable, *args]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        creationflags=CREATE_NO_WINDOW,
    )
    return CommandOutput(completed.stdout, completed.stderr, completed.returncode)


def local_cli_bridge(tool: str) -> CommandSpec:
    if tool not in {"az", "acli"}:
        raise ValueError("Unsupported CLI bridge tool")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    bridge = Path(__file__).resolve().parents[1] / "scripts" / "cli-bridge.ps1"
    if not powershell or not bridge.exists():
        raise FileNotFoundError("PowerShell CLI bridge is unavailable")
    return CommandSpec(
        prefix=[
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(bridge),
            "-Tool",
            tool,
            "-ArgumentsJson",
        ],
        json_arguments=True,
    )


def parse_json_output(output: str) -> Any:
    text = output.strip().lstrip("\ufeff")
    if not text:
        raise ValueError("Command returned no JSON")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        first = min((index for index in (text.find("{"), text.find("[")) if index >= 0), default=-1)
        if first >= 0:
            return json.loads(text[first:])
        raise


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def display_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    for key in ("displayName", "display_name", "name", "emailAddress", "uniqueName"):
        found = value.get(key)
        if found:
            return str(found)
    return None


def unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
