"""Bounded, business-free lifecycle and actual installed SDK Job regressions."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import PLUGIN_ROOT
from gxp_editor.ui_lifecycle import atomic_json, lifecycle_event, read_json
from gxp_editor.ui_launch import inspect_window_startup, launch_window, report_window_startup


class LifecycleJournalTests(unittest.TestCase):
    def test_pending_launch_without_pid_is_unknown_and_cannot_spawn_again(self):
        from gxp_editor.ui_launch import _key
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "attempts" / (("9" * 32) + ".json")
            atomic_json(path, {"startup_id": "9" * 32, "launch_key_sha256": _key("fixture"),
                "status": "starting", "created_at": time.time() - 11})
            atomic_json(root / (_key("fixture") + ".json"), {"startup_id": "9" * 32})
            result = inspect_window_startup("fixture", startup_root=root)
            self.assertEqual(result["process_state"], "unknown")
            with patch("gxp_editor.ui_launch.subprocess.Popen", side_effect=AssertionError("No new process")):
                repeated = launch_window(executable=sys.executable, script="unused", cwd=root, arguments=[],
                    launch_key="fixture", startup_root=root)
            self.assertEqual(repeated["startup_id"], "9" * 32)

    def test_proven_prelaunch_failure_keeps_exited_state_and_reason(self):
        from gxp_editor.ui_launch import _key
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "attempts" / (("d" * 32) + ".json")
            atomic_json(path, {"startup_id": "d" * 32, "launch_key_sha256": _key("fixture"),
                "status": "failed", "process_state": "exited", "reason_code": "ui_launch_failed"})
            atomic_json(root / (_key("fixture") + ".json"), {"startup_id": "d" * 32})
            result = inspect_window_startup("fixture", startup_root=root)
            self.assertEqual(result["process_state"], "exited")
            self.assertEqual(result["reason_code"], "ui_launch_failed")

    def test_late_events_cannot_erase_failure_and_heartbeat_is_not_append_spam(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "attempt.json"
            atomic_json(path, {"startup_id": "a" * 32, "generation": 2, "status": "starting"})
            self.assertFalse(lifecycle_event(path, "a" * 32, "ready", generation=1, updates={"status": "ready"}))
            self.assertTrue(lifecycle_event(path, "a" * 32, "ready", updates={"status": "ready"}))
            for _ in range(3):
                lifecycle_event(path, "a" * 32, "heartbeat", updates={"phase": "review"})
            lifecycle_event(path, "a" * 32, "failed", updates={"status": "failed", "reason_code": "fixture"})
            self.assertFalse(lifecycle_event(path, "a" * 32, "ready", updates={"status": "ready"}))
            self.assertEqual(read_json(path)["status"], "failed")
            events = [json.loads(line) for line in path.with_suffix(".events.jsonl").read_text().splitlines()]
            self.assertEqual([event["event"] for event in events], ["ready", "failed"])
            self.assertEqual([event["sequence"] for event in events], [1, 5])

    def test_unknown_identity_never_becomes_exited_or_reopenable(self):
        with tempfile.TemporaryDirectory() as folder:
            from gxp_editor.ui_launch import _key
            root = Path(folder)
            path = root / "attempts" / (("b" * 32) + ".json")
            atomic_json(path, {"startup_id": "b" * 32, "launch_key_sha256": _key("fixture"),
                "status": "ready", "pid": 12345, "process_token": "known", "created_at": time.time()})
            atomic_json(root / (_key("fixture") + ".json"), {"startup_id": "b" * 32})
            with patch("gxp_editor.ui_launch.process_identity", return_value=("unknown", None)):
                result = inspect_window_startup("fixture", startup_root=root)
            self.assertEqual(result["process_state"], "unknown")
            self.assertEqual(result["startup_status"], "process_unknown")
            self.assertFalse(result["window_opened"])
            self.assertNotIn("exit_code", read_json(path))

    def test_ready_synchronously_replaces_loading_phase(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "attempt.json"
            atomic_json(path, {"startup_id": "c" * 32, "status": "starting", "phase": "loading_artifact"})
            with patch.dict(os.environ, {"GXP_EDITOR_UI_STARTUP_RECORD": str(path), "GXP_EDITOR_UI_STARTUP_ID": "c" * 32}):
                report_window_startup("ready", phase="loading_preview", review_ready=False, preview_pending=True)
            result = read_json(path)
            self.assertEqual(result["phase"], "loading_preview")
            self.assertTrue(result["visible"])
            self.assertTrue(result["preview_pending"])
            self.assertFalse(result["review_ready"])
            self.assertEqual(read_json(path.with_suffix(".heartbeat.json"))["phase"], "loading_preview")


@unittest.skipUnless(os.name == "nt", "Actual Windows SDK Job reproduction")
class ActualSdkLifecycleTests(unittest.TestCase):
    def test_retained_handle_recognizes_actual_exit_code_259(self):
        from gxp_editor.ui_lifecycle import ProcessHandle
        child = subprocess.Popen([sys.executable, "-c", "import time,os;time.sleep(.2);os._exit(259)"],
            creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        with ProcessHandle(child.pid) as handle:
            child.wait(timeout=3)
            self.assertEqual(handle.state(), "exited")
            self.assertEqual(handle.exit_code(), 259)

    def test_conversation_ui_ready_deduplicated_and_actual_exit_observed(self):
        for scenario in ("close", "crash"):
            with self.subTest(scenario=scenario):
                payload = self._run_sdk(scenario)
                self.assertTrue(payload["window_opened"], payload)
                self.assertEqual(payload["startup_id"], payload["duplicate_startup_id"])
                self.assertEqual(payload["ui_pid"], payload["duplicate_ui_pid"])
                self.assertEqual(payload["isolation_mode"], "explicit_direct_fixture")
                self.assertTrue(payload["sdk_job_created"])
                self.assertEqual(payload["membership"], {"server": True, "ui": True})
                self.assertTrue(payload["launcher_differs_from_ui"], payload)
                record = payload["connected_exit_record"]
                self.assertEqual(record["status"], "failed" if scenario == "crash" else "closed", record)
                self.assertEqual(record["process_state"], "exited", record)
                self.assertEqual(record["exit_code"], 7 if scenario == "crash" else 0)
                self.assertEqual(record["exit_source"], "retained_ui_handle")
                self.assertEqual(record.get("normal_close_requested", False), scenario == "close")
                self.assertNotIn("observer_pid", record)
                self.assertEqual(payload["after_disconnect_ui_state"], "exited", payload)

    def test_sdk_disconnect_can_cleanup_unfinished_conversation_ui(self):
        payload = self._run_sdk("disconnect")
        self.assertTrue(payload["window_opened"], payload)
        self.assertEqual(payload["membership"], {"server": True, "ui": True})
        self.assertEqual(payload["after_disconnect_ui_state"], "exited", payload)

    def _run_sdk(self, scenario):
        fixture = PLUGIN_ROOT / "tests" / "fixtures" / "ui_lifecycle_sdk_stub.py"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ui.py").write_text("import sys\nsys.path.insert(0," + repr(str(fixture.parent)) + ")\n"
                "from ui_lifecycle_sdk_stub import ui_stub\nui_stub(" + repr(str(root)) + ")\n", encoding="utf-8")
            result = subprocess.run([sys.executable, str(fixture), "client", folder, scenario],
                capture_output=True, text=True, timeout=9, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return read_json(root / "client-result.json")


if __name__ == "__main__":
    unittest.main()
