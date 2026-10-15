"""Check legacy approval processes which predate the shared OS lock protocol."""
from __future__ import annotations

import json
import os
import re
import subprocess

from .execution_lock import ExecutionBusyError


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
