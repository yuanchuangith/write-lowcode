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
from gxp_editor.ui_lifecycle import atomic_json, lifecycle_event, process_identity, read_json
from gxp_editor.ui_launch import inspect_window_startup, launch_window, report_window_startup
from gxp_editor.ui_host import validate_request


class LifecycleJournalTests(unittest.TestCase):
    def test_pending_host_request_without_pid_is_unknown_and_cannot_spawn_again(self):
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

    def test_host_rejects_job_membership_in_production(self):
        from gxp_editor.ui_host import run_host
        with tempfile.TemporaryDirectory() as folder, \
             patch("gxp_editor.runtime_package.assert_runtime_resources"), \
             patch("gxp_editor.ui_host.in_windows_job", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "ui_host_isolation_unverified"):
                run_host(PLUGIN_ROOT, folder, maximum_seconds=.01)
    def test_proven_prelaunch_failure_keeps_exited_state_and_reason(self):
        from gxp_editor.ui_launch import _key
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "attempts" / (("d" * 32) + ".json")
            atomic_json(path, {"startup_id": "d" * 32, "launch_key_sha256": _key("fixture"),
                "status": "failed", "process_state": "exited", "reason_code": "ui_host_required"})
            atomic_json(root / (_key("fixture") + ".json"), {"startup_id": "d" * 32})
            result = inspect_window_startup("fixture", startup_root=root)
            self.assertEqual(result["process_state"], "exited")
            self.assertEqual(result["reason_code"], "ui_host_required")

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

    def test_host_rejects_arbitrary_script_environment_shape_and_log_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            startup_id = "e" * 32
            record = root / "receipts" / "attempts" / (startup_id + ".json")
            atomic_json(record, {"startup_id": startup_id, "status": "starting",
                "log_path": str(root / "receipts" / "logs" / (startup_id + ".log"))})
            request = {"startup_id": startup_id, "cwd": str(PLUGIN_ROOT), "record_path": str(record),
                "executable": sys.executable, "script": str(root / "evil.py"), "arguments": []}
            with self.assertRaisesRegex(ValueError, "fixed entrypoints"):
                validate_request(request, package_root=PLUGIN_ROOT, startup_root=root / "receipts")
            with self.assertRaisesRegex(ValueError, "environment"):
                validate_request({**request, "environment": []}, package_root=PLUGIN_ROOT,
                    startup_root=root / "receipts", fixture_script=root / "evil.py")
            atomic_json(record, {"startup_id": startup_id, "status": "starting", "log_path": str(root / "escape.log")})
            with self.assertRaisesRegex(ValueError, "log path"):
                validate_request(request, package_root=PLUGIN_ROOT, startup_root=root / "receipts",
                    fixture_script=root / "evil.py")

    def test_observer_bound_runtime_failure_is_a_specific_persistent_event(self):
        from gxp_editor.ui_observer import main
        from gxp_editor.runtime_package import RuntimeResourceError
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            record = root / "attempt.json"
            atomic_json(record, {"startup_id": "f" * 32, "status": "starting"})
            request = root / "request.json"
            atomic_json(request, {"startup_id": "f" * 32, "record_path": str(record), "cwd": str(PLUGIN_ROOT),
                "executable": sys.executable, "environment": {"GXP_EDITOR_PYTHON_IDENTITY": "{}"}})
            with patch.object(sys, "argv", ["observer", str(request)]), \
                 patch("gxp_editor.runtime_package.assert_python_interpreter", side_effect=RuntimeResourceError(
                    "plugin_runtime_interpreter_changed", "fixture changed interpreter")):
                with self.assertRaises(RuntimeResourceError):
                    main()
            self.assertEqual(read_json(record)["reason_code"], "plugin_runtime_interpreter_changed")
            self.assertEqual(read_json(record)["status"], "failed")


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

    def test_previous_direct_launch_is_killed_by_real_sdk_timeout_job(self):
        fixture = PLUGIN_ROOT / "tests" / "fixtures" / "ui_lifecycle_sdk_stub.py"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ui.py").write_text("import sys\nsys.path.insert(0," + repr(str(fixture.parent)) + ")\n"
                "from ui_lifecycle_sdk_stub import ui_stub\nui_stub()\n", encoding="utf-8")
            result = subprocess.run([sys.executable, str(fixture), "client", folder, "baseline"],
                capture_output=True, text=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = read_json(root / "client-result.json")
            self.assertEqual(payload["membership"], {"server": True, "ui": True})
            self.assertEqual(payload["after_disconnect_ui_state"], "exited", payload)

    def test_external_host_observer_and_venv_ui_survive_real_sdk_cleanup(self):
        fixture = PLUGIN_ROOT / "tests" / "fixtures" / "ui_lifecycle_sdk_stub.py"
        for scenario in ("steady", "eof", "timeout", "abrupt", "nested", "ui_crash"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "ui.py").write_text("import sys\nsys.path.insert(0," + repr(str(fixture.parent)) + ")\n"
                    "from ui_lifecycle_sdk_stub import ui_stub\nui_stub(crash=" + repr(scenario == "ui_crash") + ")\n", encoding="utf-8")
                with (root / "host.log").open("w") as log:
                    host = subprocess.Popen([sys.executable, str(fixture), "host", folder, scenario],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    try:
                        deadline = time.monotonic() + 3
                        while not (root / "receipts" / "host" / "host.json").exists() and time.monotonic() < deadline:
                            time.sleep(.02)
                        result = subprocess.run([sys.executable, str(fixture), "client", folder, scenario],
                            capture_output=True, text=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)
                        self.assertEqual(result.returncode, 19 if scenario == "abrupt" else 0,
                            result.stdout + result.stderr + (root / "host.log").read_text())
                        payload = read_json(root / "client-result.json")
                        self.assertTrue(payload["window_opened"], payload)
                        self.assertTrue(payload["sdk_job_created"])
                        self.assertEqual(payload["membership"], {"server": True, "host": False, "observer": False, "ui": False})
                        if scenario == "nested":
                            self.assertTrue(payload["breakaway_result"].startswith("denied:"), payload)
                        elif payload.get("breakaway_pid"):
                            # In this venv the SDK may bind the forwarding
                            # launcher rather than only the interpreter. Keep
                            # actual membership, not API success, as evidence.
                            self.assertIn("breakaway_in_sdk_job", payload)
                        self.assertTrue(payload["launcher_differs_from_ui"], payload)
                        if scenario != "abrupt":
                            self.assertEqual(payload["after_disconnect_ui_state"], "alive")
                            self.assertEqual(payload["after_disconnect_observer_state"], "alive")
                        record_path = root / "receipts" / "attempts" / (payload["startup_id"] + ".json")
                        deadline = time.monotonic() + 7
                        while (read_json(record_path) or {}).get("exit_source") != "retained_ui_handle" and time.monotonic() < deadline:
                            time.sleep(.025)
                        record = read_json(record_path)
                        self.assertEqual(record["status"], "failed" if scenario == "ui_crash" else "closed", record)
                        self.assertEqual(record["exit_code"], 7 if scenario == "ui_crash" else 0)
                        self.assertEqual(record["exit_source"], "retained_ui_handle")
                        self.assertEqual(record.get("normal_close_requested", False), scenario != "ui_crash")
                    finally:
                        host.wait(timeout=12)


if __name__ == "__main__":
    unittest.main()
