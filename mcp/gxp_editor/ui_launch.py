"""Bounded, file-backed UI startup receipts; never grants approval or executes work."""
from __future__ import annotations

import hashlib
import atexit
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .config import runtime_root
from .execution_lock import _os_lock, ApprovalWindowBusyError, approval_window_lock, execution_lock
from .ui_lifecycle import lifecycle_event, process_identity

_RECORD_ENV = "GXP_EDITOR_UI_STARTUP_RECORD"
_ID_ENV = "GXP_EDITOR_UI_STARTUP_ID"
_STATUSES = {"starting", "ready", "failed", "already_open", "closed"}
STARTUP_STALL_SECONDS = 30
HEARTBEAT_STALL_SECONDS = 15
_UI_LEASE = None
_UI_CLAIM = None


class _LaunchBusy(RuntimeError):
    pass


def _directory(startup_root=None) -> Path:
    return Path(startup_root) if startup_root is not None else runtime_root() / "ui-startup"


def _key(launch_key: str) -> str:
    if not isinstance(launch_key, str) or not launch_key or len(launch_key) > 1024:
        raise ValueError("launch_key must be a nonempty string of at most 1024 characters")
    return hashlib.sha256(launch_key.encode("utf-8")).hexdigest()


def _read(path: Path) -> dict[str, Any] | None:
    from .ui_lifecycle import read_json
    return read_json(path)


def _write(path: Path, value: dict[str, Any]) -> None:
    from .ui_lifecycle import atomic_json
    atomic_json(path, value)


def _process_info(pid: int) -> tuple[bool, str | None]:
    # Compatibility helper. Security decisions use the tri-state API directly.
    state, token = process_identity(pid)
    return state == "alive", token


def _process_running(pid: int) -> bool:
    return _process_info(pid)[0]


def _claimed_process(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    claim = _read(path.with_suffix(".claim.json")) or {}
    if claim.get("startup_id") == record.get("startup_id"):
        return claim
    return _read(path.with_suffix(".pid.json")) or {}


def _attempt(directory: Path, key_hash: str):
    index = _read(directory / (key_hash + ".json")) or {}
    startup_id = index.get("startup_id")
    if not isinstance(startup_id, str) or len(startup_id) != 32 or any(c not in "0123456789abcdef" for c in startup_id):
        return None, None
    path = directory / "attempts" / (startup_id + ".json")
    record = _read(path)
    if not record or record.get("startup_id") != startup_id or record.get("launch_key_sha256") != key_hash:
        return None, None
    return path, record


def _result(record=None, *, status=None, error="") -> dict[str, Any]:
    record = record or {}
    status = status or record.get("status") or "not_started"
    return {
        "window_opened": status == "ready",
        "window_starting": status == "starting",
        "startup_status": status,
        "startup_id": record.get("startup_id"),
        **{key: record[key] for key in ("generation", "event_sequence", "process_state", "visible",
            "review_ready", "preview_pending", "exit_code", "exit_at", "exit_source", "reason_code",
            "launcher_pid", "launcher_token", "launcher_exit_code", "ui_pid", "ui_token",
            "observer_pid", "observer_token", "observer_process_state", "isolation_mode", "runtime_binding",
            "queue_generation", "queue_launch_token_sha256") if key in record},
        **({"window_error": error or record["error"]} if error or record.get("error") else {}),
        **({"window_log_path": record["log_path"]} if record.get("log_path") else {}),
    }


def inspect_window_startup(launch_key: str, *, startup_root: str | Path | None = None) -> dict[str, Any]:
    directory = _directory(startup_root)
    path, record = _attempt(directory, _key(launch_key))
    if record is None:
        return _result()
    status = record.get("status")
    if status not in _STATUSES:
        return _result(record, status="failed", error="Invalid window startup receipt")
    if status in {"starting", "ready", "failed", "already_open"}:
        pid_record = _claimed_process(path, record)
        pid = record.get("pid") or pid_record.get("pid")
        if status not in {"starting", "ready"} and not pid:
            return _result(record)
        process_state, token = process_identity(pid)
        running = process_state == "alive"
        expected_token = record.get("process_token") or pid_record.get("process_token")
        if expected_token and token and token != expected_token:
            process_state = "exited"
        elif not expected_token or not token:
            process_state = "unknown" if process_state != "exited" else "exited"
        if status in {"starting", "ready"} and pid and process_state == "exited":
            actual_claim = _read(path.with_suffix(".claim.json")) or {}
            if not record.get("ui_pid") and actual_claim.get("startup_id") != record.get("startup_id"):
                lifecycle_event(path, record["startup_id"], "unclaimed_launcher_disappeared", source="inspection", updates={
                    "status": "failed", "process_state": "unknown", "exit_source": "unclaimed_ui",
                    "reason_code": "ui_never_claimed", "error": "Window launcher exited before a real UI process identity was recorded"})
                return _result(_read(path), status="failed")
            lifecycle_event(path, record["startup_id"], "exit_discovered", source="inspection", updates={
                "status": "failed", "process_state": "exited", "exit_at": time.time(),
                "exit_source": "identity_inspection", "reason_code": "ui_process_disappeared",
                "error": "Window process exited before it could remain available"})
            return _result(_read(path), status="failed")
        if status in {"starting", "ready"} and not pid and time.time() - record.get("created_at", 0) > 10:
            return {**_result(record, status="process_unknown", error="Window launcher has not supplied a verifiable process identity; its pending host request must be reconciled before retry"),
                    "process_state": "unknown", "window_recovery_available": False}
        process_running = process_state == "alive"
        if status == "failed" and record.get("process_state") == "unknown" and not record.get("ui_pid"):
            return _result(record)
        process_status = {"window_process_running": process_running, "process_state": process_state,
                          **({"window_process_id": pid} if process_running else {})}
        heartbeat = _read(path.with_suffix(".heartbeat.json")) or {}
        heartbeat_valid = (heartbeat.get("startup_id") == record.get("startup_id")
                           and heartbeat.get("pid") == pid and heartbeat.get("process_token") == token)
        if heartbeat_valid:
            process_status.update({"window_phase": heartbeat.get("phase", ""),
                                   "window_executing": heartbeat.get("executing", False),
                                   "window_heartbeat_age_seconds": max(0, time.time() - heartbeat.get("heartbeat_at", 0))})
        if (status == "ready" and process_running and record.get("heartbeat_expected")
                and time.time() - (heartbeat.get("heartbeat_at", 0) if heartbeat_valid else record.get("updated_at", record.get("created_at", 0)))
                >= HEARTBEAT_STALL_SECONDS):
            return {**_result(record, status="heartbeat_timeout", error=(
                "Window UI heartbeat stopped; its process has not been stopped. "
                "Only an unexecuted window may be recovered under execution locks.")), **process_status,
                "window_recovery_available": record.get("recovery_kind") in {"approval", "nonwriting"}}
        if (status == "starting" and process_running
                and time.time() - record.get("created_at", 0) >= STARTUP_STALL_SECONDS):
            return {**_result(record, status="startup_timeout", error=(
                f"Window startup exceeded {STARTUP_STALL_SECONDS} seconds without a visible-window receipt; "
                "its process is still running and has not been stopped")), **process_status,
                "window_recovery_available": record.get("recovery_kind") in {"approval", "nonwriting"}}
        if status in {"starting", "ready"} and pid and process_state == "unknown":
            return {**_result(record, status="process_unknown", error="Window process identity could not be verified; inspect or reconnect before reopening"),
                    **process_status, "window_recovery_available": False}
        return {**_result(record), **process_status}
    return _result(record)


def managed_window_launch() -> bool:
    return bool(os.environ.get(_RECORD_ENV) and os.environ.get(_ID_ENV))


def register_window_process() -> bool:
    """Claim the actual UI PID before expensive imports (venv may redirect PID)."""
    location, startup_id = os.environ.get(_RECORD_ENV), os.environ.get(_ID_ENV)
    if not location or not startup_id:
        return False
    path = Path(location)
    record = _read(path)
    if not record or record.get("startup_id") != startup_id:
        return False
    token = process_identity(os.getpid())[1]
    global _UI_CLAIM
    claim_identity = (str(path), startup_id, os.getpid(), token)
    if _UI_CLAIM == claim_identity:
        return True
    existing = _read(path.with_suffix(".claim.json"))
    if existing and existing != {"startup_id": startup_id, "pid": os.getpid(), "process_token": token}:
        return False
    try:
        _write(path.with_suffix(".claim.json"), {"startup_id": startup_id, "pid": os.getpid(), "process_token": token})
        lifecycle_event(path, startup_id, "claimed", updates={"pid": os.getpid(), "process_token": token,
            "ui_pid": os.getpid(), "ui_token": token, "parent_pid": os.getppid(), "process_state": "alive" if token else "unknown"})
        global _UI_LEASE
        if os.environ.get("GXP_EDITOR_RUNTIME_ROOT") and _UI_LEASE is None:
            from .runtime_package import RuntimeLease
            lease = RuntimeLease(Path(os.environ["GXP_EDITOR_RUNTIME_ROOT"]), role="ui")
            lease.__enter__()
            _UI_LEASE = lease
            atexit.register(_UI_LEASE.__exit__, None, None, None)
        _UI_CLAIM = claim_identity
        return True
    except RuntimeError as exc:
        lifecycle_event(path, startup_id, "ui_runtime_failed", updates={"status": "failed",
            "reason_code": getattr(exc, "code", "ui_runtime_registration_failed"),
            "error": "UI runtime binding failed before the review application loaded"})
        raise
    except OSError:
        return False


def report_window_heartbeat(*, phase: str, executing: bool = False) -> bool:
    """Call from the UI event loop; background activity must not fake UI health."""
    location, startup_id = os.environ.get(_RECORD_ENV), os.environ.get(_ID_ENV)
    if not location or not startup_id:
        return False
    path = Path(location)
    record = _read(path)
    if not record or record.get("startup_id") != startup_id or record.get("status") == "closed":
        return False
    phases = {"loading_artifact", "loading_preview", "review", "executing", "plan_review"}
    if phase not in phases:
        raise ValueError("Invalid window phase")
    try:
        _write(path.with_suffix(".heartbeat.json"), {"startup_id": startup_id, "pid": os.getpid(),
               "process_token": _process_info(os.getpid())[1], "heartbeat_at": time.time(),
               "phase": phase, "executing": executing is True})
        lifecycle_event(path, startup_id, "heartbeat", updates={"phase": phase, "executing": executing is True,
            "heartbeat_at": time.time(), "review_ready": phase in {"review", "plan_review"},
            "preview_pending": phase == "loading_preview"})
        return True
    except OSError:
        return False


def report_window_startup(status: str, *, error: str = "", phase: str | None = None,
                          review_ready: bool | None = None, preview_pending: bool | None = None) -> bool:
    """Called by the child after entering its UI loop, or before an error dialog."""
    if status not in _STATUSES:
        raise ValueError("Invalid window startup status")
    location, startup_id = os.environ.get(_RECORD_ENV), os.environ.get(_ID_ENV)
    if not location or not startup_id:
        return False  # Manual launches retain their existing behavior.
    path = Path(location)
    record = _read(path)
    if not record or record.get("startup_id") != startup_id:
        return False
    if not register_window_process():
        return False
    # A constructor failure followed by cleanup must not turn into a clean close.
    if record.get("status") in {"failed", "already_open"} and status == "closed":
        return True
    try:
        current_phase = phase or record.get("phase") or "review"
        if status == "ready":
            report_window_heartbeat(phase=current_phase)
        return lifecycle_event(path, startup_id, status, updates={"status": status, "pid": os.getpid(),
            "process_token": process_identity(os.getpid())[1], "error": str(error)[:2000],
            "phase": current_phase, "visible": status == "ready",
            "review_ready": (current_phase in {"review", "plan_review"}) if review_ready is None else review_ready,
            "preview_pending": current_phase == "loading_preview" if preview_pending is None else preview_pending,
            **({"ready_at": time.time()} if status == "ready" else {}),
            **({"normal_close_requested": True} if status == "closed" else {})})
    except OSError as exc:
        print(f"Window startup receipt could not be written: {exc}", file=sys.stderr)
        return False


def launch_window(*, executable: str | Path, script: str | Path, arguments: list[str],
                  cwd: str | Path, launch_key: str, timeout_seconds: float = 8.0,
                  startup_root: str | Path | None = None,
                  recovery_kind: str = "unknown", recovery_artifact_path: str | Path | None = None,
                  lifecycle_mode: str = "auto", extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    """Start once per key and wait at most eight seconds for a visible UI receipt."""
    directory, key_hash = _directory(startup_root), _key(launch_key)
    if recovery_kind not in {"unknown", "approval", "queue", "nonwriting"}:
        raise ValueError("Invalid window recovery kind")
    if recovery_kind == "approval" and recovery_artifact_path is None:
        raise ValueError("Approval recovery requires its bound artifact path")
    directory.mkdir(parents=True, exist_ok=True)
    timeout_seconds = max(0.0, min(float(timeout_seconds), 8.0))
    if lifecycle_mode not in {"auto", "direct"}:
        raise ValueError("Invalid lifecycle mode")
    extra_env = extra_env or {}
    if any(key not in {"GXP_EDITOR_QUEUE_LAUNCH_TOKEN", "GXP_EDITOR_QUEUE_GENERATION"}
           or not isinstance(value, str) or len(value) > 128 for key, value in extra_env.items()):
        raise ValueError("Invalid queue launch environment")
    if extra_env and (not extra_env.get("GXP_EDITOR_QUEUE_GENERATION", "").isdigit()
            or not extra_env.get("GXP_EDITOR_QUEUE_LAUNCH_TOKEN")):
        raise ValueError("Incomplete queue launch identity")
    try:
        with _os_lock(directory / key_hash, ".launch.lock", _LaunchBusy, "Window launch is already in progress"):
            previous = inspect_window_startup(launch_key, startup_root=directory)
            if (previous["window_opened"] or previous["window_starting"] or previous.get("window_process_running")
                    or previous.get("process_state") == "unknown" and previous.get("startup_id")):
                return {**previous, "already_open": previous["window_opened"]}
            startup_id = uuid.uuid4().hex
            path = directory / "attempts" / (startup_id + ".json")
            log = directory / "logs" / (startup_id + ".log")
            log.parent.mkdir(parents=True, exist_ok=True)
            record = {"schema_version": 2, "startup_id": startup_id, "launch_key_sha256": key_hash,
                      "generation": (previous.get("generation") or 0) + 1, "event_sequence": 0,
                      "status": "starting", "created_at": time.time(), "log_path": str(log.resolve()),
                      "recovery_kind": recovery_kind, "heartbeat_expected": recovery_kind != "unknown",
                      **({"artifact_path": str(Path(recovery_artifact_path).resolve())} if recovery_artifact_path else {})}
            if extra_env:
                record.update({"queue_generation": int(extra_env["GXP_EDITOR_QUEUE_GENERATION"]),
                    "queue_launch_token_sha256": hashlib.sha256(extra_env["GXP_EDITOR_QUEUE_LAUNCH_TOKEN"].encode("utf-8")).hexdigest()})
            if lifecycle_mode == "auto":
                from .runtime_package import runtime_binding
                try:
                    binding = runtime_binding(cwd)
                    record["runtime_binding"] = {key: binding[key] for key in (
                        "runtime_instance_id", "plugin_version", "manifest_sha256", "runtime_root")}
                    identity = binding["python_identity"]
                    record["python_identity"] = {key: identity[key] for key in (
                        "path", "executable_sha256", "version", "prefix", "venv_config_sha256") if key in identity}
                except RuntimeError:
                    # The following strict preflight preserves the precise
                    # resource error, even if this diagnostic binding is absent.
                    record["runtime_binding_status"] = "unknown"
            _write(path, record)
            _write(directory / (key_hash + ".json"), {"startup_id": startup_id})
            lifecycle_event(path, startup_id, "created", source="mcp")
            if os.name == "nt" and lifecycle_mode == "auto":
                from .ui_host import submit_request
                from .runtime_package import assert_runtime_resources, runtime_binding
                try:
                    assert_runtime_resources(Path(cwd))
                except RuntimeError as exc:
                    lifecycle_event(path, startup_id, "runtime_preflight_failed", source="mcp", updates={
                        "status": "failed", "process_state": "exited",
                        "reason_code": getattr(exc, "code", "plugin_runtime_resource_changed"),
                        "error": "UI runtime preflight failed; reconnect to a complete fixed runtime"})
                    return {**inspect_window_startup(launch_key, startup_root=directory),
                            "error_details": getattr(exc, "details", {}), "reconnect_required": True}
                request = {"startup_id": startup_id, "record_path": str(path.resolve()),
                    "executable": str(Path(executable).resolve()), "script": str(Path(script).resolve()),
                    "arguments": arguments, "cwd": str(Path(cwd).resolve()),
                    "environment": {**{key: value for key, value in os.environ.items() if key.startswith("GXP_EDITOR_RUNTIME_")
                        or key in {"GXP_EDITOR_PYTHON_IDENTITY", "GXP_LOWCODE_EDITOR_STATE", "GXP_LOWCODE_EDITOR_CONFIG"}}, **extra_env}}
                if not submit_request(request, directory):
                    lifecycle_event(path, startup_id, "host_required", source="mcp", updates={
                        "status": "failed", "reason_code": "ui_host_required",
                        "process_state": "exited",
                        "error": "Start the local UI host explicitly outside the MCP client, then retry"})
                    host_executable = Path(executable)
                    if host_executable.name.lower() == "pythonw.exe":
                        host_executable = host_executable.with_name("python.exe")
                    return {**inspect_window_startup(launch_key, startup_root=directory),
                        "ui_host_required": True, "ui_host_command": [str(host_executable),
                            str(Path(cwd) / "scripts" / "ui_host.py"), "--runtime-root", str(Path(cwd).resolve())],
                        "runtime_binding": runtime_binding(cwd)}
                deadline = time.monotonic() + timeout_seconds
                while True:
                    result = inspect_window_startup(launch_key, startup_root=directory)
                    if not result["window_starting"] or time.monotonic() >= deadline:
                        return result
                    time.sleep(.025)
            try:
                with log.open("ab") as stderr:
                    process = subprocess.Popen(
                        [str(executable), str(script), *arguments], cwd=str(cwd),
                        env={**os.environ, **extra_env, _RECORD_ENV: str(path.resolve()), _ID_ENV: startup_id},
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        start_new_session=os.name != "nt",
                    )
                _write(path.with_suffix(".pid.json"), {"pid": process.pid, "process_token": _process_info(process.pid)[1],
                                                       "startup_id": startup_id})
                lifecycle_event(path, startup_id, "launcher_started", source="mcp", updates={
                    "launcher_pid": process.pid, "launcher_token": _process_info(process.pid)[1],
                    "isolation_mode": "posix_session" if os.name != "nt" else "explicit_direct_fixture"})
                # Compatibility path for POSIX and explicitly isolated fixtures.
                def wait_launcher():
                    code = process.wait()
                    lifecycle_event(path, startup_id, "launcher_exited", source="launcher_wait", updates={
                        "launcher_pid": process.pid, "launcher_exit_code": code, "launcher_exit_at": time.time()})
                threading.Thread(target=wait_launcher, daemon=True, name="gxp-window-wait").start()
            except OSError as exc:
                _write(path, {**record, "status": "failed", "error": str(exc)[:2000]})
                return inspect_window_startup(launch_key, startup_root=directory)
            deadline = time.monotonic() + timeout_seconds
            while True:
                result = inspect_window_startup(launch_key, startup_root=directory)
                if not result["window_starting"]:
                    return result
                exit_code = process.poll()
                if exit_code is not None:
                    current = _read(path) or record
                    _write(path, {**current, "status": "failed", "error": f"Window process exited during startup (code {exit_code}); inspect its local log"})
                    return inspect_window_startup(launch_key, startup_root=directory)
                if time.monotonic() >= deadline:
                    return {**result, "window_error": "Window is still starting; no visible-window receipt has arrived"}
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    except _LaunchBusy:
        result = inspect_window_startup(launch_key, startup_root=directory)
        return result if result["startup_id"] else _result(status="starting", error="Another launch is in progress")


def recover_stalled_window(launch_key: str, startup_id: str, *, verify_unexecuted=None,
                           startup_root: str | Path | None = None) -> dict[str, Any]:
    """Release a proven stalled local UI only; no approval/session state is changed.

    Approval callers must freshly check state and artifact inside the supplied
    execution-lock scope. Queues are intentionally excluded until every active
    item can be locked atomically with queue membership.
    """
    from .process_check import terminate_exact_process
    directory, key_hash = _directory(startup_root), _key(launch_key)
    stopped = False
    try:
        with _os_lock(directory / key_hash, ".launch.lock", _LaunchBusy, "Window launch is busy"):
            path, record = _attempt(directory, key_hash)
            if record is None or record.get("startup_id") != startup_id:
                raise ValueError("Window startup identity changed; inspect the current window first")
            current = inspect_window_startup(launch_key, startup_root=directory)
            if current["startup_status"] not in {"startup_timeout", "heartbeat_timeout"}:
                raise ValueError("Only a stalled live window can be recovered")
            kind = record.get("recovery_kind")
            if kind not in {"approval", "nonwriting"}:
                raise ValueError("This window has no safe automatic recovery binding")
            pid_record = _read(path.with_suffix(".claim.json")) or {}
            pid = record.get("pid") or pid_record.get("pid")
            token = record.get("process_token") or pid_record.get("process_token")
            if (pid_record.get("startup_id") != startup_id or pid_record.get("pid") != pid
                    or pid_record.get("process_token") != token):
                raise ValueError("Window child identity does not match its original launch token")
            if not token or _process_info(pid) != (True, token):
                raise ValueError("Window process creation identity could not be verified")
            if current.get("window_executing") is True:
                raise ValueError("Window reports a save in progress; recovery refused")
            with ExitStack() as locks:
                artifact_path = record.get("artifact_path")
                if kind == "approval":
                    if not artifact_path or not callable(verify_unexecuted):
                        raise ValueError("Approval recovery requires a fresh unexecuted-state check")
                    locks.enter_context(execution_lock(artifact_path))
                    if verify_unexecuted(Path(artifact_path)) is not True:
                        raise ValueError("Execution has started or its result is unknown; recovery refused")
                    # If startup never claimed its approval-window lock, hold it
                    # now so another review cannot race this recovery. Otherwise
                    # verify the frozen holder released it after exact-PID stop.
                    try:
                        locks.enter_context(approval_window_lock(artifact_path))
                        approval_lock_claimed = True
                    except ApprovalWindowBusyError:
                        approval_lock_claimed = False
                # Re-read health after acquiring locks; a recovered UI is retained.
                refreshed = inspect_window_startup(launch_key, startup_root=directory)
                if refreshed["startup_status"] not in {"startup_timeout", "heartbeat_timeout"}:
                    raise ValueError("Window became responsive; recovery is no longer needed")
                if refreshed.get("window_executing") is True:
                    raise ValueError("Window reports a save in progress; recovery refused")
                if not terminate_exact_process(pid, token):
                    raise ValueError("Exact window process could not be safely stopped")
                stopped = True
                if kind == "approval" and not approval_lock_claimed:
                    locks.enter_context(approval_window_lock(artifact_path))
                lifecycle_event(path, startup_id, "recovered", source="recovery", updates={
                    "status": "closed", "recovered_unexecuted_window": True})
            return {**_result(record, status="closed"), "window_recovered": True,
                    "window_process_stopped": True, "reopen_required": True,
                    "write_executed": False, "approval_granted": False}
    except (RuntimeError, ValueError, OSError) as exc:
        return {"window_recovered": False, "write_executed": False, "approval_granted": False,
                "window_process_stopped": stopped, "window_error": str(exc), "startup_id": startup_id}
