"""Small, local-only UI lifecycle journal and retained process observation.

No business payloads belong here. A startup journal never approves or executes.
"""
from __future__ import annotations

import ctypes
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .execution_lock import _os_lock


class ReceiptBusy(RuntimeError):
    pass


def read_json(path):
    deadline = time.monotonic() + .2
    while True:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except PermissionError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(.005)
        except (OSError, ValueError):
            return None


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        deadline = time.monotonic() + .5
        while True:
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                # Windows readers briefly deny DELETE sharing while loading a
                # snapshot. Retry the atomic replace, never rewrite in place.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.01)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def receipt_lock(path):
    deadline = time.monotonic() + 2
    while True:
        lock = _os_lock(path, ".events.lock", ReceiptBusy, "UI lifecycle receipt is busy")
        try:
            lock.__enter__()
            break
        except ReceiptBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.01)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


# Fixed fields only: never permit arbitrary exception text or tool arguments in
# the journal. The bounded local stderr file remains the diagnostic attachment.
_FIELDS = {"status", "pid", "process_token", "parent_pid", "launcher_pid", "launcher_token",
           "observer_pid", "observer_token", "ui_pid", "ui_token", "exit_code", "exit_at",
           "exit_source", "launcher_exit_code", "launcher_exit_at", "phase", "visible",
           "review_ready", "preview_pending", "heartbeat_at", "executing", "ready_at",
           "normal_close_requested", "error", "reason_code", "process_state", "isolation_mode",
           "observer_process_state", "monitor_state", "ui_in_job", "observer_in_job", "recovered_unexecuted_window",
           "queue_generation", "queue_launch_token_sha256"}


def lifecycle_event(path, startup_id, kind, *, source="ui", updates=None, generation=None):
    """Serialize state transitions; append key events, retain heartbeat latest only."""
    path = Path(path)
    updates = {k: v for k, v in (updates or {}).items() if k in _FIELDS}
    if "error" in updates:
        updates["error"] = str(updates["error"])[:2000]
    with receipt_lock(path):
        record = read_json(path)
        if not record or record.get("startup_id") != startup_id:
            return False
        if generation is not None and record.get("generation", 1) != generation:
            return False
        # Terminal failures cannot be hidden by a late ready/heartbeat/close.
        terminal = record.get("status") in {"failed", "closed", "already_open"}
        if terminal and kind in {"claimed", "ready", "heartbeat", "closed"}:
            return kind == "closed"
        sequence = record.get("event_sequence", 0) + 1
        stamp = time.time()
        current = {**record, **updates, "event_sequence": sequence, "updated_at": stamp}
        if kind != "heartbeat":
            # A fixed schema and bounded values keep repeated failure callbacks
            # from growing without bound. At cap retain the first critical
            # events and current state; never rotate business audit records.
            event = {"schema_version": 1, "startup_id": startup_id,
                     "generation": record.get("generation", 1), "sequence": sequence,
                     "observed_at": stamp, "event": kind, "source": source,
                     **{k: v for k, v in updates.items() if k != "error"}}
            journal = path.with_suffix(".events.jsonl")
            if not journal.exists() or journal.stat().st_size < 256 * 1024:
                with journal.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        atomic_json(path, current)
        return True


def process_identity(pid):
    """Return alive/exited/unknown and creation token; access denial is unknown."""
    if type(pid) is not int or pid <= 0:
        return "unknown", None
    if os.name == "nt":
        with ProcessHandle(pid) as process:
            return process.state(), process.token
    try:
        os.kill(pid, 0)
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return ("exited" if fields[0] == "Z" else "alive"), fields[19]
    except ProcessLookupError:
        return "exited", None
    except (OSError, IndexError):
        return "unknown", None


class ProcessHandle:
    """Keep a Windows handle, including the real UI beyond venv forwarding."""
    def __init__(self, pid):
        self.pid, self.handle, self.token = pid, None, None
        self.open_error = None
        if os.name != "nt" or type(pid) is not int or pid <= 0:
            return
        from ctypes import wintypes as w
        self.w = w
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = (w.HANDLE,)
        self.kernel.GetExitCodeProcess.argtypes = (w.HANDLE, ctypes.POINTER(w.DWORD))
        self.kernel.GetExitCodeProcess.restype = w.BOOL
        self.kernel.WaitForSingleObject.argtypes = (w.HANDLE, w.DWORD)
        self.kernel.WaitForSingleObject.restype = w.DWORD
        self.kernel.GetProcessTimes.argtypes = (w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4))
        self.kernel.GetProcessTimes.restype = w.BOOL
        self.handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, pid)
        if not self.handle:
            self.open_error = ctypes.get_last_error()
            return
        fields = [w.FILETIME() for _ in range(4)]
        if self.kernel.GetProcessTimes(self.handle, *[ctypes.byref(f) for f in fields]):
            self.token = str((fields[0].dwHighDateTime << 32) | fields[0].dwLowDateTime)

    def state(self):
        if os.name != "nt":
            return process_identity(self.pid)[0]
        if not self.handle:
            return "exited" if self.open_error == 87 else "unknown"
        wait = self.kernel.WaitForSingleObject(self.handle, 0)
        return "exited" if wait == 0 else "alive" if wait == 258 else "unknown"

    def exit_code(self):
        if os.name != "nt" or not self.handle:
            return None
        code = self.w.DWORD()
        return code.value if self.state() == "exited" and self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)) else None

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
