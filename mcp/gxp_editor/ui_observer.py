"""Independent UI observer: the host, never MCP, owns this process."""
from __future__ import annotations

import os
import json
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

from .ui_lifecycle import (ProcessHandle, atomic_json, in_windows_job, lifecycle_event,
                           process_identity, read_json)


def observe(request):
    path = Path(request["record_path"])
    startup_id = request["startup_id"]
    record = read_json(path)
    if not record or record.get("startup_id") != startup_id:
        return 2
    runtime = Path(request["cwd"])
    if request.get("fixture"):
        lease = nullcontext()
    else:
        from .runtime_package import RuntimeLease, assert_runtime_resources
        environment = request.get("environment", {})
        # Validate before acquiring observer lease, then freeze that same
        # runtime for all lazy imports and the actual UI invocation.
        from .runtime_package import assert_python_interpreter
        frozen = json.loads(environment.get("GXP_EDITOR_PYTHON_IDENTITY", "null"))
        assert_python_interpreter(Path(request["executable"]), frozen)
        os.environ.update({key: value for key, value in environment.items()
                           if key.startswith("GXP_EDITOR_RUNTIME_") or key == "GXP_EDITOR_PYTHON_IDENTITY"})
        assert_runtime_resources(runtime)
        lease = RuntimeLease(runtime, role="observer")
    with lease:
        lifecycle_event(path, startup_id, "observer_started", source="observer", updates={
            "observer_pid": os.getpid(), "observer_token": process_identity(os.getpid())[1],
            "observer_process_state": "alive", "observer_in_job": in_windows_job(),
            "isolation_mode": request["isolation_mode"]})
        log = Path(record["log_path"])
        env = {**os.environ, **request.get("environment", {}),
               "GXP_EDITOR_UI_STARTUP_RECORD": str(path), "GXP_EDITOR_UI_STARTUP_ID": startup_id}
        process = None
        claimed = None
        ui = None
        try:
            with log.open("ab") as stderr:
                process = subprocess.Popen([request["executable"], request["script"], *request["arguments"]],
                    cwd=request["cwd"], env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=stderr,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            launcher_token = process_identity(process.pid)[1]
            atomic_json(path.with_suffix(".pid.json"), {"startup_id": startup_id,
                "pid": process.pid, "process_token": launcher_token})
            lifecycle_event(path, startup_id, "launcher_started", source="observer", updates={
                "launcher_pid": process.pid, "launcher_token": launcher_token})
            launcher_recorded = False
            deadline = time.monotonic() + request.get("maximum_seconds", 7 * 24 * 3600)
            while True:
                if claimed is None:
                    candidate = read_json(path.with_suffix(".claim.json")) or {}
                    if candidate.get("startup_id") == startup_id:
                        handle = ProcessHandle(candidate.get("pid"))
                        if (handle.token and handle.token == candidate.get("process_token")) or os.name != "nt":
                            claimed, ui = candidate, handle
                            lifecycle_event(path, startup_id, "ui_observed", source="observer", updates={
                                "ui_pid": candidate["pid"], "ui_token": candidate.get("process_token"),
                                "ui_in_job": in_windows_job(candidate["pid"])})
                        else:
                            handle.close()
                code = process.poll()
                if code is not None and not launcher_recorded:
                    launcher_recorded = True
                    lifecycle_event(path, startup_id, "launcher_exited", source="observer", updates={
                        "launcher_exit_code": code, "launcher_exit_at": time.time()})
                if ui is not None and ui.state() == "exited":
                    current = read_json(path) or {}
                    normal = current.get("normal_close_requested") is True
                    lifecycle_event(path, startup_id, "ui_exited", source="observer", updates={
                        "status": current.get("status") if normal or current.get("status") in {"failed", "already_open"} else "failed",
                        "process_state": "exited", "exit_code": ui.exit_code(), "exit_at": time.time(),
                        "exit_source": "retained_ui_handle" if os.name == "nt" else "process_observation",
                        **({} if normal or current.get("reason_code") else {"reason_code": "ui_process_exited", "error": "Window process exited before it could remain available"})})
                    break
                if code is not None and claimed is None:
                    lifecycle_event(path, startup_id, "unclaimed_launcher_exited", source="observer", updates={
                        "status": "failed", "process_state": "unknown", "exit_code": None,
                        "exit_source": "unclaimed_ui", "reason_code": "ui_never_claimed",
                        "error": "Window launcher exited before an actual UI identity was claimed"})
                    break
                if time.monotonic() >= deadline:
                    # Fixtures are bounded. Production maximum does not kill a
                    # possibly executing UI; leave its last evidence unknown.
                    if request.get("fixture"):
                        process.terminate()
                        process.wait(timeout=3)
                    lifecycle_event(path, startup_id, "observer_budget_expired", source="observer", updates={
                        "observer_process_state": "exited", "process_state": "unknown",
                        "reason_code": "observer_budget_expired"})
                    break
                time.sleep(.025)
        except OSError:
            lifecycle_event(path, startup_id, "launch_failed", source="observer", updates={
                "status": "failed", "reason_code": "ui_launch_failed",
                "error": "UI launch failed; inspect its local log", "process_state": "unknown"})
            raise
        finally:
            if ui is not None:
                ui.close()
            lifecycle_event(path, startup_id, "observer_exited", source="observer", updates={
                "observer_process_state": "exited"})
    return 0


def main():
    request = read_json(Path(sys.argv[1]))
    if not request:
        return 2
    try:
        return observe(request)
    except Exception as exc:
        lifecycle_event(Path(request["record_path"]), request["startup_id"], "observer_failed", source="observer",
            updates={"status": "failed", "reason_code": getattr(exc, "code", "ui_observer_failed"),
                     "error": "UI observer could not start its bound runtime; inspect its local log",
                     "observer_process_state": "exited", "process_state": "unknown"})
        raise
