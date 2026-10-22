"""Bounded, file-backed UI startup receipts; never grants approval or executes work."""
from __future__ import annotations

import hashlib
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

_RECORD_ENV = "GXP_EDITOR_UI_STARTUP_RECORD"
_ID_ENV = "GXP_EDITOR_UI_STARTUP_ID"
_STATUSES = {"starting", "ready", "failed", "already_open", "closed"}
STARTUP_STALL_SECONDS = 30
HEARTBEAT_STALL_SECONDS = 15


class _LaunchBusy(RuntimeError):
    pass


def _directory(startup_root=None) -> Path:
    return Path(startup_root) if startup_root is not None else runtime_root() / "ui-startup"


def _key(launch_key: str) -> str:
    if not isinstance(launch_key, str) or not launch_key or len(launch_key) > 1024:
        raise ValueError("launch_key must be a nonempty string of at most 1024 characters")
    return hashlib.sha256(launch_key.encode("utf-8")).hexdigest()


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _process_info(pid: int) -> tuple[bool, str | None]:
    if type(pid) is not int or pid <= 0:
        return False, None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                          ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                          ctypes.POINTER(wintypes.FILETIME))
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION only.
        if not handle:
            return ctypes.get_last_error() == 5, None  # Access denied is not proof of exit.
        try:
            code = wintypes.DWORD()
            running = bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            token = None
            if kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
                token = str((created.dwHighDateTime << 32) | created.dwLowDateTime)
            return running, token
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            return fields[0] != "Z", fields[19]
        except (OSError, IndexError):
            return True, None
    except PermissionError:
        return True, None
    except ProcessLookupError:
        return False, None


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
        running, token = _process_info(pid)
        expected_token = record.get("process_token") or pid_record.get("process_token")
        if status in {"starting", "ready"} and pid and (not running or expected_token and token != expected_token):
            return _result(record, status="failed", error="Window process exited before it could remain available")
        if status in {"starting", "ready"} and not pid and time.time() - record.get("created_at", 0) > 10:
            return _result(record, status="failed", error="Window launcher did not record a running process")
        process_running = bool(running and (not expected_token or token == expected_token))
        process_status = {"window_process_running": process_running,
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
    token = _process_info(os.getpid())[1]
    existing = _read(path.with_suffix(".claim.json"))
    if existing and existing != {"startup_id": startup_id, "pid": os.getpid(), "process_token": token}:
        return False
    try:
        _write(path.with_suffix(".claim.json"), {"startup_id": startup_id, "pid": os.getpid(), "process_token": token})
        return True
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
        return True
    except OSError:
        return False


def report_window_startup(status: str, *, error: str = "") -> bool:
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
        _write(path, {**record, "status": status, "pid": os.getpid(), "process_token": _process_info(os.getpid())[1],
                      "updated_at": time.time(), "error": str(error)[:2000]})
        return True
    except OSError as exc:
        print(f"Window startup receipt could not be written: {exc}", file=sys.stderr)
        return False


def launch_window(*, executable: str | Path, script: str | Path, arguments: list[str],
                  cwd: str | Path, launch_key: str, timeout_seconds: float = 8.0,
                  startup_root: str | Path | None = None,
                  recovery_kind: str = "unknown", recovery_artifact_path: str | Path | None = None) -> dict[str, Any]:
    """Start once per key and wait at most eight seconds for a visible UI receipt."""
    directory, key_hash = _directory(startup_root), _key(launch_key)
    if recovery_kind not in {"unknown", "approval", "queue", "nonwriting"}:
        raise ValueError("Invalid window recovery kind")
    if recovery_kind == "approval" and recovery_artifact_path is None:
        raise ValueError("Approval recovery requires its bound artifact path")
    directory.mkdir(parents=True, exist_ok=True)
    timeout_seconds = max(0.0, min(float(timeout_seconds), 8.0))
    try:
        with _os_lock(directory / key_hash, ".launch.lock", _LaunchBusy, "Window launch is already in progress"):
            previous = inspect_window_startup(launch_key, startup_root=directory)
            if previous["window_opened"] or previous["window_starting"] or previous.get("window_process_running"):
                return {**previous, "already_open": previous["window_opened"]}
            startup_id = uuid.uuid4().hex
            path = directory / "attempts" / (startup_id + ".json")
            log = directory / "logs" / (startup_id + ".log")
            log.parent.mkdir(parents=True, exist_ok=True)
            record = {"schema_version": 1, "startup_id": startup_id, "launch_key_sha256": key_hash,
                      "status": "starting", "created_at": time.time(), "log_path": str(log.resolve()),
                      "recovery_kind": recovery_kind, "heartbeat_expected": recovery_kind != "unknown",
                      **({"artifact_path": str(Path(recovery_artifact_path).resolve())} if recovery_artifact_path else {})}
            _write(path, record)
            _write(directory / (key_hash + ".json"), {"startup_id": startup_id})
            try:
                with log.open("ab") as stderr:
                    process = subprocess.Popen(
                        [str(executable), str(script), *arguments], cwd=str(cwd),
                        env={**os.environ, _RECORD_ENV: str(path.resolve()), _ID_ENV: startup_id},
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                _write(path.with_suffix(".pid.json"), {"pid": process.pid, "process_token": _process_info(process.pid)[1],
                                                       "startup_id": startup_id})
                # Retain/reap the child without blocking tool calls or terminating UI.
                threading.Thread(target=process.wait, daemon=True, name="gxp-window-wait").start()
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
                _write(path, {**record, "status": "closed", "updated_at": time.time(),
                              "recovered_unexecuted_window": True})
            return {**_result(record, status="closed"), "window_recovered": True,
                    "window_process_stopped": True, "reopen_required": True,
                    "write_executed": False, "approval_granted": False}
    except (RuntimeError, ValueError, OSError) as exc:
        return {"window_recovered": False, "write_executed": False, "approval_granted": False,
                "window_process_stopped": stopped, "window_error": str(exc), "startup_id": startup_id}
