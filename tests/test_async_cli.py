import asyncio
import json
import os
import subprocess
import sys

import pytest

from connectors import base
from connectors.base import CommandSpec, async_run_command


@pytest.fixture
def started_processes(monkeypatch):
    processes = []
    create = asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(base.asyncio, "create_subprocess_exec", record)
    return processes


async def wait_for_file(path, *, nonempty=False):
    async def wait():
        while not path.exists() or (nonempty and not path.read_text().strip()):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), 5)


@pytest.mark.asyncio
async def test_async_command_preserves_utf8_environment_and_literal_arguments(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    arguments = ['space and "quotes"', "$(do-not-run)", "a&b", "emoji 😀"]
    script = (
        "import json, os, sys; "
        "print(json.dumps([os.environ['NO_COLOR'], sys.argv[1:]])); "
        "sys.stderr.buffer.write(b'warning\\xff'); sys.exit(7)"
    )

    output = await async_run_command(sys.executable, ["-c", script, *arguments], 5)

    assert json.loads(output.stdout) == ["1", arguments]
    assert output.stderr == "warning\ufffd"
    assert output.returncode == 7


@pytest.mark.asyncio
async def test_async_command_matches_text_mode_newlines_and_keeps_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "custom")
    script = "import os, sys; sys.stdout.buffer.write(b'one\\r\\ntwo\\rthree\\n'); print(os.environ['NO_COLOR'])"

    output = await async_run_command(sys.executable, ["-c", script], 5)

    assert output.stdout == "one\ntwo\nthree\ncustom\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("json_arguments", [False, True])
async def test_async_command_supports_command_specs(json_arguments):
    script = "import json, sys; print(json.dumps(sys.argv[1:]))"
    arguments = ["jira", "search", 'summary ~ "example"']
    command = CommandSpec([sys.executable, "-c", script], json_arguments=json_arguments)

    output = await async_run_command(command, arguments, 5)

    expected = [json.dumps(arguments)] if json_arguments else arguments
    assert json.loads(output.stdout) == expected
    assert output.returncode == 0


@pytest.mark.asyncio
async def test_async_timeout_kills_and_reaps_the_command(started_processes):
    command = ["-c", "import time; time.sleep(60)"]

    with pytest.raises(subprocess.TimeoutExpired) as failure:
        await asyncio.wait_for(async_run_command(sys.executable, command, 0.1), 10)

    process = started_processes[0]
    assert process.returncode is not None
    assert await process.wait() == process.returncode
    assert failure.value.cmd == [sys.executable, *command]
    assert failure.value.timeout == 0.1


@pytest.mark.asyncio
async def test_async_cancellation_kills_and_reaps_the_command(tmp_path, started_processes):
    ready = tmp_path / "ready"
    script = "import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"
    task = asyncio.create_task(async_run_command(sys.executable, ["-c", script, str(ready)], 60))
    await wait_for_file(ready)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)

    process = started_processes[0]
    assert process.returncode is not None
    assert await process.wait() == process.returncode


@pytest.mark.asyncio
async def test_cancellation_during_startup_still_reaps_the_command(monkeypatch):
    create = asyncio.create_subprocess_exec
    created = asyncio.Event()
    release_handle = asyncio.Event()
    processes = []

    async def delayed_handle(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        created.set()
        await release_handle.wait()
        return process

    monkeypatch.setattr(base.asyncio, "create_subprocess_exec", delayed_handle)
    task = asyncio.create_task(async_run_command(sys.executable, ["-c", "import time; time.sleep(60)"], 60))
    await asyncio.wait_for(created.wait(), 5)
    task.cancel()
    release_handle.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)

    assert processes[0].returncode is not None
    assert await processes[0].wait() == processes[0].returncode


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_cleanup(monkeypatch):
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    original_cleanup = base._terminate_command

    async def delayed_cleanup(startup, communication):
        cleanup_started.set()
        await finish_cleanup.wait()
        return await original_cleanup(startup, communication)

    monkeypatch.setattr(base, "_terminate_command", delayed_cleanup)
    task = asyncio.create_task(async_run_command(sys.executable, ["-c", "import time; time.sleep(60)"], 60))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.wait_for(cleanup_started.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended command startup")
@pytest.mark.asyncio
async def test_windows_job_assignment_failure_reaps_the_suspended_command(monkeypatch, started_processes):
    def reject_assignment(self, pid):
        raise OSError("Synthetic job assignment refused")

    monkeypatch.setattr(base._WindowsCommandJob, "assign_and_resume", reject_assignment)
    with pytest.raises(OSError, match="Synthetic job assignment refused"):
        await async_run_command(sys.executable, ["-c", "import time; time.sleep(60)"], 60)

    assert started_processes[0].returncode is not None
    assert await started_processes[0].wait() == started_processes[0].returncode


@pytest.mark.skipif(os.name != "nt", reason="Windows CLI bridge process-tree cleanup")
@pytest.mark.asyncio
@pytest.mark.parametrize("exit_parent", [False, True])
async def test_windows_cancellation_stops_the_bridge_child(tmp_path, started_processes, exit_parent):
    import ctypes
    from ctypes import wintypes

    child_pid_file = tmp_path / "child-pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        + ("sys.exit(0)" if exit_parent else "time.sleep(60)")
    )
    task = asyncio.create_task(async_run_command(sys.executable, ["-c", script, str(child_pid_file)], 60))
    await wait_for_file(child_pid_file, nonempty=True)
    child_pid = int(child_pid_file.read_text())
    if exit_parent:
        async def wait_for_parent_exit():
            while started_processes[0].returncode is None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_parent_exit(), 5)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    child_handle = kernel.OpenProcess(0x00100000, False, child_pid)
    assert child_handle
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert started_processes[0].returncode is not None
        assert kernel.WaitForSingleObject(child_handle, 0) == 0
    finally:
        kernel.CloseHandle(child_handle)
