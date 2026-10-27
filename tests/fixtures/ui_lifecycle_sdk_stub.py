"""Business-free conversation-bound UI regression against the installed MCP SDK.

Only temporary receipts, heartbeat files and a fixed signal are used. There is
no production host, approval, database connection or business artifact.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACKAGE / "mcp"))
from gxp_editor.ui_lifecycle import atomic_json, process_identity, read_json


def ui_stub(folder):
    from gxp_editor.ui_launch import register_window_process, report_window_heartbeat, report_window_startup
    if not register_window_process():
        raise RuntimeError("fixture UI did not claim its process")
    report_window_startup("ready", phase="review")
    deadline = time.monotonic() + 10
    signal = Path(folder) / "ui-signal.json"
    while time.monotonic() < deadline:
        report_window_heartbeat(phase="review")
        action = (read_json(signal) or {}).get("action")
        if action in {"close", "crash"}:
            if action == "crash":
                os._exit(7)
            report_window_startup("closed")
            return
        time.sleep(.025)
    # A failed client cannot leave a permanent fixture window behind.
    os._exit(8)


def server(folder, scenario):
    from mcp.server.fastmcp import FastMCP
    from gxp_editor.ui_launch import inspect_window_startup, launch_window
    mcp = FastMCP("conversation-lifecycle-test-only")

    @mcp.tool()
    def open_stub() -> dict[str, Any]:
        # direct only bypasses production package preflight for this temporary
        # fixture; process ownership and observation use the real launch path.
        result = launch_window(executable=sys.executable, script=folder / "ui.py", arguments=[],
            cwd=PACKAGE, launch_key="sdk-stub", startup_root=folder / "receipts", recovery_kind="nonwriting",
            lifecycle_mode="direct")
        return {**result, "server_pid": os.getpid()}

    @mcp.tool()
    def inspect_stub() -> dict[str, Any]:
        return inspect_window_startup("sdk-stub", startup_root=folder / "receipts")

    mcp.run(transport="stdio")
    if scenario == "disconnect":
        time.sleep(10)  # Exercise the SDK's real timeout and Job cleanup path.


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

    # Shorten the grace period, not the SDK Job implementation. All fixture
    # processes and temporary files stay within this bounded client lifetime.
    with patch.object(stdio, "_create_platform_compatible_process", side_effect=capture), \
         patch.object(stdio, "PROCESS_TERMINATION_TIMEOUT", .25):
        async with stdio.stdio_client(StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve()), "server", str(folder), scenario],
            env={"PYTHONPATH": str(PACKAGE / "mcp")})) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("open_stub", {})
                payload = result.structuredContent
                repeated = (await session.call_tool("open_stub", {})).structuredContent
                record_path = folder / "receipts" / "attempts" / (payload["startup_id"] + ".json")
                record = read_json(record_path)
                job = getattr(captured[0], "_job_object", None)
                membership = {}
                for name, pid in {"server": payload["server_pid"], "ui": record["ui_pid"]}.items():
                    handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, pid)
                    try:
                        membership[name] = bool(win32job.IsProcessInJob(handle, job))
                    finally:
                        handle.Close()
                payload.update({"sdk_job_created": bool(job), "membership": membership,
                    "duplicate_startup_id": repeated["startup_id"],
                    "duplicate_ui_pid": repeated.get("ui_pid"),
                    "launcher_differs_from_ui": record["launcher_pid"] != record["ui_pid"]})
                if scenario != "disconnect":
                    atomic_json(folder / "ui-signal.json", {"action": scenario})
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        record = read_json(record_path)
                        if record.get("exit_source") == "retained_ui_handle":
                            break
                        await asyncio.sleep(.025)
                    payload["connected_exit_record"] = record
                atomic_json(folder / "client-result.json", payload)
    # Disconnect may end an unfinished UI. This is an explicit boundary, not
    # an instruction to create an independent service or survive the dialogue.
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        state = process_identity(record["ui_pid"])[0]
        if state == "exited":
            break
        await asyncio.sleep(.025)
    payload["after_disconnect_ui_state"] = state
    atomic_json(folder / "client-result.json", payload)


def main():
    mode, folder, scenario = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    if mode == "server":
        server(folder, scenario)
    else:
        asyncio.run(client(folder, scenario))


if __name__ == "__main__":
    main()
