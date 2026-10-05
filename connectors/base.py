from __future__ import annotations

import asyncio
import json
import os
import signal
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


async def async_run_command(
    executable: str | CommandSpec, args: list[str], timeout: float
) -> CommandOutput:
    """Run a CLI without a worker thread and reap it before cancellation returns."""
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    if isinstance(executable, CommandSpec):
        command = [*executable.prefix, json.dumps(args)] if executable.json_arguments else [*executable.prefix, *args]
    else:
        command = [executable, *args]
    # Shield startup as well: cancellation can arrive after the OS creates the
    # child but before asyncio returns its Process handle.
    startup = asyncio.create_task(_start_command(command, env))
    communication = None
    try:
        process, job = await asyncio.shield(startup)
        communication = asyncio.create_task(process.communicate())
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout)
        if job is not None:
            await job.terminate()
            job.close()
    except (asyncio.TimeoutError, asyncio.CancelledError) as failure:
        cleanup = asyncio.create_task(_terminate_command(startup, communication))
        canceled_during_cleanup = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Finish reaping even if an outer refresh cancels us again.
                canceled_during_cleanup = True
        stdout, stderr = cleanup.result()
        if isinstance(failure, asyncio.CancelledError):
            raise
        if canceled_during_cleanup:
            raise asyncio.CancelledError from failure
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from failure
    finally:
        # Close the owned job even if a native cleanup API itself fails.
        if startup.done() and not startup.cancelled() and startup.exception() is None:
            _, finished_job = startup.result()
            if finished_job is not None:
                finished_job.close()
    # Match subprocess.run(text=True), including universal newline handling.
    decode = lambda value: value.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return CommandOutput(decode(stdout), decode(stderr), process.returncode)


async def _start_command(command, env):
    job = _WindowsCommandJob() if os.name == "nt" else None
    options = ({"creationflags": CREATE_NO_WINDOW | 0x00000004}  # CREATE_SUSPENDED
               if job is not None else {"start_new_session": True})
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, **options,
        )
        if job is not None:
            # Assign before the suspended CLI can spawn a launcher or bridge
            # child. The job retains descendants even after ancestors exit.
            job.assign_and_resume(process.pid)
        return process, job
    except BaseException:
        if job is not None:
            job.close()
        if process is not None:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            await process.wait()
        raise


async def _terminate_command(startup, communication) -> tuple[bytes, bytes]:
    """Kill the command group, drain its pipes, and reap the direct child."""
    try:
        process, job = await startup
    except OSError:
        # Startup cleans any suspended child before returning its failure.
        return b"", b""
    try:
        if communication is None:
            communication = asyncio.create_task(process.communicate())
        if job is not None:
            await job.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        stdout, stderr = await communication
        await process.wait()
        return stdout, stderr
    finally:
        if job is not None:
            job.close()


class _WindowsCommandJob:
    """Own a Windows command and all descendants, including exited launchers."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.thread_type = ThreadEntry
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "Thread32First": ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            "Thread32Next": ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes = arguments
            function.restype = result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_and_resume(self, pid):
        process = self.kernel.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not process:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(process)
        snapshot = self.kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == self.wintypes.HANDLE(-1).value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            entry = self.thread_type()
            entry.dwSize = self.ctypes.sizeof(entry)
            present = self.kernel.Thread32First(snapshot, self.ctypes.byref(entry))
            while present:
                if entry.th32OwnerProcessID == pid:
                    thread = self.kernel.OpenThread(0x0002, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise self.ctypes.WinError(self.ctypes.get_last_error())
                    try:
                        if self.kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise self.ctypes.WinError(self.ctypes.get_last_error())
                        return
                    finally:
                        self.kernel.CloseHandle(thread)
                present = self.kernel.Thread32Next(snapshot, self.ctypes.byref(entry))
            raise OSError("Suspended CLI main thread was unavailable")
        finally:
            self.kernel.CloseHandle(snapshot)

    def _process_handles(self):
        capacity = 16
        while True:
            class ProcessIds(self.ctypes.Structure):
                _fields_ = [
                    ("NumberOfAssignedProcesses", self.wintypes.DWORD),
                    ("NumberOfProcessIdsInList", self.wintypes.DWORD),
                    ("ProcessIdList", self.ctypes.c_size_t * capacity),
                ]

            processes = ProcessIds()
            success = self.kernel.QueryInformationJobObject(
                self.handle, 3, self.ctypes.byref(processes), self.ctypes.sizeof(processes), None,
            )
            if success:
                break
            if self.ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            capacity = max(capacity * 2, processes.NumberOfAssignedProcesses)
        handles = []
        for pid in processes.ProcessIdList[:processes.NumberOfProcessIdsInList]:
            handle = self.kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if handle:
                handles.append(handle)
        return handles

    async def terminate(self):
        if not self.handle:
            return
        # Retain each member's handle before termination. Job accounting can
        # report zero active members before their process handles are signaled.
        handles = self._process_handles()
        try:
            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            pending = handles
            while pending:
                pending = [handle for handle in pending if self.kernel.WaitForSingleObject(handle, 0) == 258]
                if pending:
                    await asyncio.sleep(0.01)
        finally:
            for handle in handles:
                self.kernel.CloseHandle(handle)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


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


async def completed_within(coroutines, timeout):
    """Preserve successful results at the deadline and drain canceled tasks."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    if not tasks:
        return []
    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        return [task.result() for task in tasks if task in done and not task.cancelled() and task.exception() is None]
    finally:
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
