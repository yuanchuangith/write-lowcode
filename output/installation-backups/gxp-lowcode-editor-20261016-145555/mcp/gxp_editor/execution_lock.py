"""Independent OS locks for execution and approval windows, released on exit."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


class ExecutionBusyError(RuntimeError):
    pass


class ApprovalWindowBusyError(RuntimeError):
    pass


@contextmanager
def _os_lock(artifact_path: str | Path, suffix: str, busy_error: type[RuntimeError], message: str):
    lock_path = Path(str(artifact_path) + suffix)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise busy_error(message) from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def execution_lock(artifact_path: str | Path):
    return _os_lock(
        artifact_path, ".execution.lock", ExecutionBusyError,
        "事务仍在执行，请等待执行结束后再核对。",
    )


def approval_window_lock(artifact_path: str | Path):
    """Claim this transaction's window without preventing its own execution."""
    return _os_lock(
        artifact_path, ".approval-window.lock", ApprovalWindowBusyError,
        "该审批流程已有窗口打开。同一个审批流程只能打开一个窗口；请关闭原窗口后再打开。",
    )
