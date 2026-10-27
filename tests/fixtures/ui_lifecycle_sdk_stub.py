"""Real SDK Job reproduction fixture. No editor state or database is imported."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch
from typing import Any

PACKAGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACKAGE / "mcp"))
from gxp_editor.ui_lifecycle import atomic_json, process_identity, read_json


def ui_stub(crash=False):
    from gxp_editor.ui_launch import register_window_process, report_window_startup, report_window_heartbeat
    register_window_process()
    report_window_startup("ready", phase="review")
    deadline = time.monotonic() + 4.5
    while time.monotonic() < deadline:
        report_window_heartbeat(phase="review")
        time.sleep(.1)
        if crash and time.monotonic() > deadline - 3:
            os._exit(7)
    report_window_startup("closed")


def server(folder, scenario):
    from mcp.server.fastmcp import FastMCP
    from gxp_editor.ui_launch import launch_window
    import win32api
    import win32job
    mcp = FastMCP("lifecycle-test-only")
    nested = None

    @mcp.tool()
    def open_stub() -> dict[str, Any]:
        nonlocal nested
        if scenario == "nested":
            nested = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(nested, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(nested, win32job.JobObjectExtendedLimitInformation, info)
            win32job.AssignProcessToJobObject(nested, win32api.GetCurrentProcess())
        breakaway = "unexpected_success"
        breakaway_pid = None
        try:
            child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(6)"],
                creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB | subprocess.CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            breakaway_pid = child.pid
        except OSError as exc:
            breakaway = f"denied:{exc.winerror}"
        # Only resource packaging is substituted. The actual SDK transport,
        # launch receipts, host, observer and UI processes are real.
        with patch("gxp_editor.runtime_package.assert_runtime_resources", return_value=None):
            result = launch_window(executable=sys.executable, script=folder / "ui.py", arguments=[],
                cwd=PACKAGE, launch_key="sdk-stub", startup_root=folder / "receipts", recovery_kind="nonwriting",
                lifecycle_mode="direct" if scenario == "baseline" else "auto")
        return {**result, "server_pid": os.getpid(), "server_token": process_identity(os.getpid())[1],
                "breakaway_result": breakaway, "breakaway_pid": breakaway_pid}

    mcp.run(transport="stdio")
    if scenario in {"timeout", "baseline"}:
        time.sleep(8)  # Force the installed SDK's real timeout/Job cleanup.


async def client(folder, scenario):
    import mcp.client.stdio as stdio
    from mcp import ClientSession, StdioServerParameters
    import win32api
    import win32con
    import win32job
    captured = []
    original = stdio._create_platform_compatible_process

    async def capture(**kwargs):
        process = await original(**kwargs)
        captured.append(process)
        return process

    with patch.object(stdio, "_create_platform_compatible_process", side_effect=capture):
        async with stdio.stdio_client(StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve()), "server", str(folder), scenario],
            env={"PYTHONPATH": str(PACKAGE / "mcp")})) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("open_stub", {})
                payload = result.structuredContent
                job = getattr(captured[0], "_job_object", None)
                info = read_json(folder / "receipts" / "host" / "host.json")
                record = read_json(folder / "receipts" / "attempts" / (payload["startup_id"] + ".json"))
                membership = {}
                processes = {"server": payload["server_pid"], "ui": record["ui_pid"]}
                if scenario != "baseline":
                    processes.update({"host": info["pid"], "observer": record["observer_pid"]})
                for name, pid in processes.items():
                    handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, pid)
                    try:
                        membership[name] = bool(win32job.IsProcessInJob(handle, job))
                    finally:
                        handle.Close()
                if payload.get("breakaway_pid"):
                    handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, payload["breakaway_pid"])
                    try:
                        payload["breakaway_in_sdk_job"] = bool(win32job.IsProcessInJob(handle, job))
                    finally:
                        handle.Close()
                limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
                payload.update({"sdk_job_created": bool(job), "membership": membership,
                    "sdk_job_flags": limits["BasicLimitInformation"]["LimitFlags"],
                    "launcher_differs_from_ui": record.get("launcher_pid", (read_json(folder / "receipts" / "attempts" / (payload["startup_id"] + ".pid.json")) or {})["pid"]) != record["ui_pid"]})
                atomic_json(folder / "client-result.json", payload)
                if scenario == "abrupt":
                    os._exit(19)  # Close the real SDK Job handle without finally.
                if scenario == "steady":
                    await asyncio.sleep(.2)
    payload["after_disconnect_ui_state"] = process_identity(record["ui_pid"])[0]
    if scenario == "baseline":
        deadline = time.monotonic() + .8
        while payload["after_disconnect_ui_state"] == "alive" and time.monotonic() < deadline:
            await asyncio.sleep(.02)
            payload["after_disconnect_ui_state"] = process_identity(record["ui_pid"])[0]
    if scenario != "baseline":
        payload["after_disconnect_observer_state"] = process_identity(record["observer_pid"])[0]
    atomic_json(folder / "client-result.json", payload)


def main():
    mode, folder, scenario = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    if mode == "server":
        server(folder, scenario)
    elif mode == "host":
        from gxp_editor.ui_host import run_host
        run_host(PACKAGE, folder / "receipts", maximum_seconds=12, fixture_script=folder / "ui.py")
    else:
        asyncio.run(client(folder, scenario))


if __name__ == "__main__":
    main()
