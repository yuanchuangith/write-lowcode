"""Check legacy approval processes which predate the shared OS lock protocol."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

from .execution_lock import ExecutionBusyError


def terminate_exact_process(pid: int, process_token: str, *, timeout_seconds: float = 3.0) -> bool:
    """Stop only the creation identity already validated by a startup receipt.

    This is deliberately not a process-tree kill: compiler children cannot save
    data and must never broaden a window-recovery request to unrelated work.
    """
    if type(pid) is not int or pid <= 0 or pid == os.getpid() or not process_token:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, pid)
        if not handle:
            return False
        try:
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return False
            actual = str((created.dwHighDateTime << 32) | created.dwLowDateTime)
            if actual != process_token:
                return False
            return bool(kernel.TerminateProcess(handle, 2)) and kernel.WaitForSingleObject(
                handle, int(max(0, min(timeout_seconds, 5)) * 1000)) == 0
        finally:
            kernel.CloseHandle(handle)
    # Linux pidfd retains the process identity across PID reuse.
    import signal
    from .ui_launch import _process_info
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    try:
        descriptor = os.pidfd_open(pid)
        try:
            if _process_info(pid) != (True, process_token):
                return False
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            deadline = time.monotonic() + max(0, min(timeout_seconds, 5))
            while time.monotonic() < deadline:
                if _process_info(pid) != (True, process_token):
                    return True
                time.sleep(0.02)
            return False
        finally:
            os.close(descriptor)
    except OSError:
        return False


def approval_process_ids(transaction_id: str) -> list[int]:
    if not isinstance(transaction_id, str) or not transaction_id or len(transaction_id) > 128:
        raise ValueError("Invalid transaction identity")
    if os.name == "nt":
        command = r'''
$editorMarker = $env:GXP_EDITOR_CHECK_TRANSACTION
$editorProcesses = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%.exe'" -ErrorAction Stop | Where-Object {
    $_.CommandLine -and $_.CommandLine -match '(?i)approval_app\.py(?:["\s]|$)' -and $_.CommandLine.Contains($editorMarker)
} | Select-Object -ExpandProperty ProcessId)
ConvertTo-Json -InputObject $editorProcesses -Compress
'''
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            env={**os.environ, "GXP_EDITOR_CHECK_TRANSACTION": transaction_id},
            capture_output=True, text=True, timeout=8, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return [int(value) for value in json.loads(result.stdout)]
    result = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=5, check=True)
    matches = []
    for line in result.stdout.splitlines():
        values = line.strip().split(None, 1)
        if len(values) == 2 and re.search(r"approval_app\.py(?:\s|$)", values[1]) and transaction_id in values[1]:
            matches.append(int(values[0]))
    return matches


def assert_approval_process_stopped(transaction_id: str) -> None:
    try:
        processes = approval_process_ids(transaction_id)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ExecutionBusyError("无法确认旧审批进程是否退出，请关闭审批程序后重试进程检查。") from exc
    if processes:
        raise ExecutionBusyError("该流程的审批程序仍在运行，请先关闭原审批窗口，再接手或释放占用。")
