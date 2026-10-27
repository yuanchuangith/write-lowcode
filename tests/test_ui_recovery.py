"""Offline window recovery must never substitute for transaction recovery."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import MCP_ROOT
from gxp_editor.execution_lock import ExecutionBusyError, execution_lock, approval_window_lock
from gxp_editor.ui_launch import (
    HEARTBEAT_STALL_SECONDS, STARTUP_STALL_SECONDS, _process_info, inspect_window_startup,
    launch_window, recover_stalled_window, report_window_heartbeat,
)


class StalledWindowRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.receipts = self.root / "receipts"
        self.artifact = self.root / "approval.json"
        self.artifact.write_text('{"execution_started":false}', encoding="utf-8")
        self.addCleanup(self.wait_for_fixture_children)

    def wait_for_fixture_children(self):
        deadline = time.monotonic() + 3
        for path in self.receipts.glob("attempts/*.claim.json"):
            child = json.loads(path.read_text(encoding="utf-8"))
            if child["pid"] == os.getpid():
                continue
            while _process_info(child["pid"]) == (True, child["process_token"]) and time.monotonic() < deadline:
                time.sleep(0.02)
        time.sleep(0.05)

    def receipt(self, *, kind="approval", status="starting", executing=False):
        startup_id = "a" * 32
        key_hash = hashlib.sha256(b"fixture").hexdigest()
        path = self.receipts / "attempts" / (startup_id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        token = _process_info(os.getpid())[1]
        record = {"schema_version": 1, "startup_id": startup_id, "launch_key_sha256": key_hash,
                  "status": status, "created_at": time.time() - STARTUP_STALL_SECONDS - 1,
                  "updated_at": time.time() - HEARTBEAT_STALL_SECONDS - 1,
                  "pid": os.getpid(), "process_token": token, "recovery_kind": kind,
                  "heartbeat_expected": True, "artifact_path": str(self.artifact)}
        path.write_text(json.dumps(record), encoding="utf-8")
        path.with_suffix(".pid.json").write_text(json.dumps({"startup_id": startup_id, "pid": os.getpid(),
                                                            "process_token": token}), encoding="utf-8")
        path.with_suffix(".claim.json").write_text(path.with_suffix(".pid.json").read_text(encoding="utf-8"), encoding="utf-8")
        (self.receipts / (key_hash + ".json")).write_text(json.dumps({"startup_id": startup_id}), encoding="utf-8")
        if executing:
            path.with_suffix(".heartbeat.json").write_text(json.dumps({"startup_id": startup_id,
                "pid": os.getpid(), "process_token": token, "heartbeat_at": record["updated_at"],
                "phase": "executing", "executing": True}), encoding="utf-8")
        return startup_id, path

    def recover(self, startup_id, verify=None):
        return recover_stalled_window("fixture", startup_id, verify_unexecuted=verify, startup_root=self.receipts)

    def test_ready_process_with_stale_ui_heartbeat_is_not_claimed_as_open(self):
        _, path = self.receipt(status="ready")
        result = inspect_window_startup("fixture", startup_root=self.receipts)
        self.assertEqual(result["startup_status"], "heartbeat_timeout")
        self.assertTrue(result["window_process_running"])
        self.assertFalse(result["window_opened"])
        self.assertTrue(result["window_recovery_available"])
        env = {"GXP_EDITOR_UI_STARTUP_RECORD": str(path), "GXP_EDITOR_UI_STARTUP_ID": "a" * 32}
        with patch.dict(os.environ, env):
            self.assertTrue(report_window_heartbeat(phase="review"))
        self.assertTrue(inspect_window_startup("fixture", startup_root=self.receipts)["window_opened"])

    def test_recovery_holds_execution_lock_and_changes_no_artifact_or_approval(self):
        startup_id, _ = self.receipt()
        original = self.artifact.read_bytes()
        def verify(path):
            self.assertEqual(path, self.artifact)
            with self.assertRaises(ExecutionBusyError):
                with execution_lock(path):
                    pass
            return True
        with patch("gxp_editor.process_check.terminate_exact_process", return_value=True) as stop:
            result = self.recover(startup_id, verify)
        self.assertTrue(result["window_recovered"])
        self.assertFalse(result["write_executed"])
        self.assertFalse(result["approval_granted"])
        self.assertTrue(result["reopen_required"])
        self.assertEqual(self.artifact.read_bytes(), original)
        stop.assert_called_once()

    def test_unexecuted_verification_failure_never_stops_process(self):
        startup_id, _ = self.receipt()
        with patch("gxp_editor.process_check.terminate_exact_process") as stop:
            self.assertFalse(self.recover(startup_id, lambda path: False)["window_recovered"])
            stop.assert_not_called()

    def test_busy_execution_never_stops_process(self):
        startup_id, _ = self.receipt()
        with execution_lock(self.artifact), patch("gxp_editor.process_check.terminate_exact_process") as stop:
            result = self.recover(startup_id, Mock(return_value=True))
        self.assertFalse(result["window_recovered"])
        stop.assert_not_called()

    def test_save_in_progress_heartbeat_never_stops_process(self):
        startup_id, _ = self.receipt(status="ready", executing=True)
        with patch("gxp_editor.process_check.terminate_exact_process") as stop:
            self.assertFalse(self.recover(startup_id, lambda path: True)["window_recovered"])
        stop.assert_not_called()

    def test_queue_and_unknown_window_cannot_be_force_recovered(self):
        for kind in ("queue", "unknown"):
            with self.subTest(kind=kind):
                startup_id, path = self.receipt(kind=kind)
                with patch("gxp_editor.process_check.terminate_exact_process") as stop:
                    self.assertFalse(self.recover(startup_id, lambda path: True)["window_recovered"])
                stop.assert_not_called()

    def test_startup_id_or_original_pid_token_mismatch_never_stops_process(self):
        startup_id, path = self.receipt()
        with patch("gxp_editor.process_check.terminate_exact_process") as stop:
            self.assertFalse(self.recover("b" * 32, lambda path: True)["window_recovered"])
            sidecar = json.loads(path.with_suffix(".claim.json").read_text(encoding="utf-8"))
            sidecar["process_token"] = "different-process"
            path.with_suffix(".claim.json").write_text(json.dumps(sidecar), encoding="utf-8")
            self.assertFalse(self.recover(startup_id, lambda path: True)["window_recovered"])
        stop.assert_not_called()

    def test_real_stalled_nonwriting_child_can_be_stopped_without_a_session(self):
        child = self.root / "nonwriting.py"
        child.write_text("import sys,time\nsys.path.insert(0," + repr(str(MCP_ROOT)) + ")\n"
                         "from gxp_editor.ui_launch import register_window_process\n"
                         "register_window_process()\ntime.sleep(2)\n", encoding="utf-8")
        result = launch_window(lifecycle_mode="direct", executable=sys.executable, script=child, arguments=[], cwd=self.root,
                               launch_key="fixture", startup_root=self.receipts, timeout_seconds=0,
                               recovery_kind="nonwriting")
        path = self.receipts / "attempts" / (result["startup_id"] + ".json")
        deadline = time.monotonic() + 1
        while (not path.with_suffix(".claim.json").exists() or
               not json.loads(path.read_text(encoding="utf-8")).get("ui_pid")) and time.monotonic() < deadline:
            time.sleep(0.02)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["created_at"] = time.time() - STARTUP_STALL_SECONDS - 1
        path.write_text(json.dumps(record), encoding="utf-8")
        recovered = self.recover(result["startup_id"])
        self.assertTrue(recovered["window_recovered"], recovered)
        self.assertFalse(recovered["write_executed"])
        time.sleep(0.1)  # Windows venv launcher also releases its inherited log handle.

    def test_real_unexecuted_approval_child_releases_its_window_lock(self):
        child = self.root / "approval-fixture.py"
        child.write_text("import sys,time\nsys.path.insert(0," + repr(str(MCP_ROOT)) + ")\n"
            "from gxp_editor.ui_launch import register_window_process,report_window_startup\n"
            "from gxp_editor.execution_lock import approval_window_lock\n"
            "register_window_process()\nwith approval_window_lock(" + repr(str(self.artifact)) + "):\n"
            " report_window_startup('ready')\n time.sleep(2)\n", encoding="utf-8")
        result = launch_window(lifecycle_mode="direct", executable=sys.executable, script=child, arguments=[], cwd=self.root,
                               launch_key="fixture", startup_root=self.receipts, timeout_seconds=1,
                               recovery_kind="approval", recovery_artifact_path=self.artifact)
        self.assertTrue(result["window_opened"], result)
        path = self.receipts / "attempts" / (result["startup_id"] + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        record["updated_at"] = time.time() - HEARTBEAT_STALL_SECONDS - 1
        path.write_text(json.dumps(record), encoding="utf-8")
        heartbeat_path = path.with_suffix(".heartbeat.json")
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        heartbeat["heartbeat_at"] = record["updated_at"]
        heartbeat_path.write_text(json.dumps(heartbeat), encoding="utf-8")
        recovered = self.recover(result["startup_id"], lambda path: path.resolve() == self.artifact.resolve())
        self.assertTrue(recovered["window_recovered"], recovered)
        with approval_window_lock(self.artifact):
            pass
        self.assertEqual(json.loads(self.artifact.read_text(encoding="utf-8")), {"execution_started": False})
        time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
