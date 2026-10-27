"""UI startup tests use temporary receipts and fixture children, never sessions."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.helpers import MCP_ROOT, PLUGIN_ROOT
from gxp_editor.ui_launch import (
    STARTUP_STALL_SECONDS, _process_info, _process_running,
    inspect_window_startup, launch_window, report_window_startup,
)


class WindowStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.startup = self.root / "receipts"
        self.addCleanup(self.wait_for_fixture_children)

    def wait_for_fixture_children(self):
        deadline = time.monotonic() + 3
        for path in self.startup.glob("attempts/*.pid.json"):
            pid = json.loads(path.read_text(encoding="utf-8"))["pid"]
            while _process_running(pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(_process_running(pid), "Fixture child did not exit by itself")
            record_path = path.with_suffix("").with_suffix(".json")
            while (record_path.exists() and not json.loads(record_path.read_text(encoding="utf-8")).get("launcher_exit_at")
                   and time.monotonic() < deadline):
                time.sleep(.02)
            while (record_path.exists() and json.loads(record_path.read_text(encoding="utf-8")).get("monitor_state") != "stopped"
                   and time.monotonic() < deadline):
                time.sleep(.02)

    def launch(self, source, *, key="test-window", timeout=2, mode="direct"):
        script = self.root / "child.py"
        script.write_text(
            f"import sys,time\nsys.path.insert(0,{str(MCP_ROOT)!r})\n"
            "from gxp_editor.ui_launch import report_window_startup\n" + source,
            encoding="utf-8")
        return launch_window(lifecycle_mode=mode, executable=sys.executable, script=script, arguments=[], cwd=self.root,
                             launch_key=key, startup_root=self.startup, timeout_seconds=timeout)

    def test_default_auto_opens_from_conversation_without_external_host(self):
        with patch("gxp_editor.runtime_package.assert_runtime_resources") as resources, \
             patch("gxp_editor.runtime_package.assert_python_interpreter") as interpreter, \
             patch.dict(sys.modules, {"gxp_editor.ui_host": None, "gxp_editor.ui_observer": None}):
            result = self.launch("report_window_startup('ready')\ntime.sleep(.3)\nreport_window_startup('closed')\n", mode="auto")
        self.assertTrue(result["window_opened"], result)
        self.assertEqual(result["isolation_mode"], "conversation_bound")
        resources.assert_called_once_with(self.root)
        interpreter.assert_called_once_with(Path(sys.executable))
        self.wait_for_status("test-window", "closed")

    def test_default_auto_rejects_runtime_or_interpreter_drift_before_spawn(self):
        from gxp_editor.runtime_package import RuntimeResourceError
        for entry, code in (("assert_runtime_resources", "plugin_runtime_resource_changed"),
                            ("assert_python_interpreter", "plugin_runtime_interpreter_changed")):
            with self.subTest(code=code), \
                 patch("gxp_editor.runtime_package.assert_runtime_resources"), \
                 patch("gxp_editor.runtime_package.assert_python_interpreter"), \
                 patch("gxp_editor.runtime_package." + entry, side_effect=RuntimeResourceError(code, "fixture")), \
                 patch("gxp_editor.ui_launch.subprocess.Popen", side_effect=AssertionError("No process permitted")):
                result = self.launch("", mode="auto", key=code)
            self.assertEqual(result["reason_code"], code)
            self.assertEqual(result["process_state"], "exited")
            self.assertFalse(result["window_opened"])

    def test_completed_window_stops_monitor_without_starting_another_process(self):
        with patch("gxp_editor.ui_launch.subprocess.Popen", wraps=subprocess.Popen) as spawn:
            result = self.launch("report_window_startup('ready')\ntime.sleep(.3)\nreport_window_startup('closed')\n")
        self.assertTrue(result["window_opened"], result)
        self.assertEqual(spawn.call_count, 1)
        self.wait_for_fixture_children()
        record_path = self.startup / "attempts" / (result["startup_id"] + ".json")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["monitor_state"], "stopped")
        self.assertEqual(record["process_state"], "exited")
        self.assertEqual(record["status"], "closed")
        self.assertNotIn("observer_pid", record)

    def wait_for_status(self, key, status):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = inspect_window_startup(key, startup_root=self.startup)
            if result["startup_status"] == status:
                return result
            time.sleep(0.02)
        self.fail(f"Startup status did not become {status}: {result}")

    def test_child_receipt_is_required_and_closed_window_is_not_available(self):
        result = self.launch("report_window_startup('ready')\ntime.sleep(0.3)\nreport_window_startup('closed')\n")
        self.assertTrue(result["window_opened"])
        self.assertFalse(result["window_starting"])
        with patch("gxp_editor.ui_launch.subprocess.Popen", side_effect=AssertionError("No duplicate child")):
            repeated = launch_window(lifecycle_mode="direct", executable=sys.executable, script="unused.py", arguments=[], cwd=self.root,
                                     launch_key="test-window", startup_root=self.startup)
        self.assertEqual(repeated["startup_id"], result["startup_id"])
        self.assertTrue(repeated["window_opened"])
        closed = self.wait_for_status("test-window", "closed")
        self.assertFalse(closed["window_opened"])
        self.assertEqual(closed["startup_id"], result["startup_id"])

    def test_startup_crash_reports_failure_and_keeps_local_stderr(self):
        result = self.launch("raise RuntimeError('fixture startup crashed')\n")
        self.assertFalse(result["window_opened"])
        self.assertFalse(result["window_starting"])
        self.assertEqual(result["startup_status"], "failed")
        self.assertIn("fixture startup crashed", Path(result["window_log_path"]).read_text(encoding="utf-8"))

    def test_pending_startup_is_bounded_and_same_key_does_not_spawn_again(self):
        before = time.monotonic()
        result = self.launch("time.sleep(0.5)\n", timeout=0.05)
        self.assertLess(time.monotonic() - before, 1.0)
        self.assertFalse(result["window_opened"])
        self.assertTrue(result["window_starting"])
        with patch("gxp_editor.ui_launch.subprocess.Popen", side_effect=AssertionError("No duplicate child")):
            repeated = launch_window(lifecycle_mode="direct", executable=sys.executable, script="unused.py", arguments=[], cwd=self.root,
                                     launch_key="test-window", startup_root=self.startup)
        self.assertEqual(repeated["startup_id"], result["startup_id"])
        self.assertTrue(repeated["window_starting"])
        self.assertFalse(self.wait_for_status("test-window", "failed")["window_opened"])

    def test_failed_receipt_arrives_before_a_child_blocks_on_error_ui(self):
        result = self.launch("report_window_startup('failed',error='initialization failed')\ntime.sleep(0.25)\n")
        self.assertFalse(result["window_opened"])
        self.assertEqual(result["window_error"], "initialization failed")
        # The fixture exits by itself; the launcher never kills a UI process.
        time.sleep(0.3)

    def test_busy_window_without_a_ready_receipt_is_not_claimed_as_open(self):
        result = self.launch("report_window_startup('already_open',error='transaction window is busy')\n")
        self.assertFalse(result["window_opened"])
        self.assertFalse(result["window_starting"])
        self.assertEqual(result["startup_status"], "already_open")

    def test_ready_process_disappearance_is_detected_on_inspection(self):
        result = self.launch("report_window_startup('ready')\ntime.sleep(0.2)\n")
        self.assertTrue(result["window_opened"])
        failed = self.wait_for_status("test-window", "failed")
        self.assertFalse(failed["window_opened"])
        self.assertIn("exited", failed["window_error"])
        retried = self.launch("report_window_startup('ready')\ntime.sleep(0.2)\nreport_window_startup('closed')\n")
        self.assertTrue(retried["window_opened"])
        self.assertNotEqual(retried["startup_id"], result["startup_id"])
        self.wait_for_status("test-window", "closed")

    def test_missing_executable_is_reported_without_claiming_a_window(self):
        result = launch_window(lifecycle_mode="direct", executable=self.root / "missing-python.exe", script="unused.py", arguments=[],
                               cwd=self.root, launch_key="missing", startup_root=self.startup)
        self.assertEqual(result["startup_status"], "failed")
        self.assertFalse(result["window_opened"])
        self.assertFalse(result["window_starting"])
        self.assertEqual(result["process_state"], "exited")

    def test_manual_launch_has_no_receipt_side_effects(self):
        with patch.dict(os.environ, {}, clear=True):
            report_window_startup("ready")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_stalled_live_startup_reports_timeout_without_mutation_or_duplicate_spawn(self):
        key = "stalled-fixture"
        startup_id = uuid.uuid4().hex
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = self.startup / "attempts" / (startup_id + ".json")
        path.parent.mkdir(parents=True)
        log_path = str(self.root / "fixture.log")
        record = {"schema_version": 1, "startup_id": startup_id, "launch_key_sha256": key_hash,
                  "status": "starting", "created_at": time.time() - STARTUP_STALL_SECONDS - 5,
                  "pid": os.getpid(), "process_token": _process_info(os.getpid())[1], "log_path": log_path}
        path.write_text(json.dumps(record), encoding="utf-8")
        (self.startup / (key_hash + ".json")).write_text(json.dumps({"startup_id": startup_id}), encoding="utf-8")
        original_receipt = path.read_bytes()
        result = inspect_window_startup(key, startup_root=self.startup)
        self.assertEqual(result["startup_status"], "startup_timeout")
        self.assertFalse(result["window_opened"])
        self.assertFalse(result["window_starting"])
        self.assertTrue(result["window_process_running"])
        self.assertEqual(result["window_process_id"], os.getpid())
        self.assertEqual(result["window_log_path"], log_path)
        self.assertIn(str(STARTUP_STALL_SECONDS), result["window_error"])
        self.assertEqual(path.read_bytes(), original_receipt)
        with patch("gxp_editor.ui_launch.subprocess.Popen", side_effect=AssertionError("No duplicate child")):
            repeated = launch_window(lifecycle_mode="direct", executable=sys.executable, script="unused.py", arguments=[], cwd=self.root,
                                     launch_key=key, startup_root=self.startup)
        self.assertEqual(repeated["startup_status"], "startup_timeout")
        self.assertEqual(repeated["startup_id"], startup_id)
        self.assertEqual(path.read_bytes(), original_receipt)
        with patch.dict(os.environ, {"GXP_EDITOR_UI_STARTUP_RECORD": str(path), "GXP_EDITOR_UI_STARTUP_ID": startup_id}):
            self.assertTrue(report_window_startup("ready"))
        recovered = inspect_window_startup(key, startup_root=self.startup)
        self.assertTrue(recovered["window_opened"])
        self.assertFalse(recovered["window_starting"])
        self.assertEqual(recovered["startup_status"], "ready")
        self.assertEqual(recovered["startup_id"], startup_id)
        self.assertEqual(recovered["window_process_id"], os.getpid())
        self.assertEqual(recovered["window_log_path"], log_path)


class WindowScriptReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def load(name, filename):
            spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / "scripts" / filename)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        cls.approval = load("approval_startup_receipts", "approval_app.py")
        cls.abort = load("abort_startup_receipts", "abort_session_app.py")

    def setUp(self):
        for module in (self.approval, self.abort):
            patcher = patch.object(module, "managed_window_launch", return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_queue_os_lock_releases_even_when_control_state_is_unavailable(self):
        lock = SimpleNamespace(__exit__=Mock())
        controller = SimpleNamespace(_window_lock=None, _queue_lock=lock, state=Mock(), queue_id="fixture")
        with patch.dict(os.environ, {"GXP_EDITOR_QUEUE_GENERATION": "2", "GXP_EDITOR_QUEUE_LAUNCH_TOKEN": "fixture"}), \
             patch("gxp_editor.queue_protocol.release_launch", side_effect=RuntimeError("fixture state unavailable")):
            with self.assertRaisesRegex(RuntimeError, "fixture state unavailable"):
                self.approval.ApprovalQueueWindow._release_window_lock(controller)
        lock.__exit__.assert_called_once_with(None, None, None)
        self.assertIsNone(controller._queue_lock)

    def test_qt_ready_is_reported_from_the_event_loop_after_show(self):
        queued, reports = [], []
        root = Mock()
        root.isVisible.return_value = True
        def event_loop():
            self.assertEqual(reports, [])
            root.show.assert_called_once()
            queued.pop()()
        controller = SimpleNamespace(root=root, qt_app=SimpleNamespace(exec=event_loop), close=Mock())
        with patch.object(self.approval.QTimer, "singleShot", side_effect=lambda delay, callback: queued.append(callback)), \
             patch.object(self.approval, "report_window_startup", side_effect=lambda status, **kw: reports.append(status)):
            self.approval.ApprovalWindow.run(controller)
        self.assertEqual(reports, ["ready", "closed"])

    def test_tk_ready_waits_for_the_event_loop_and_a_viewable_window(self):
        queued, reports = [], []
        root = Mock()
        root.winfo_viewable.return_value = True
        root.after_idle.side_effect = queued.append
        def event_loop():
            self.assertEqual(reports, [])
            queued.pop()()
        root.mainloop.side_effect = event_loop
        with patch.object(self.abort, "report_window_startup", side_effect=lambda status, **kw: reports.append(status)):
            self.abort.AbortSessionWindow.run(SimpleNamespace(root=root))
        self.assertEqual(reports, ["ready", "closed"])

    def test_approval_failure_is_reported_before_the_modal_error(self):
        events = []
        with patch.object(self.approval, "ApprovalWindow", side_effect=RuntimeError("startup failed")), \
             patch.object(self.approval.sys, "argv", ["approval_app.py", "fixture"]), \
             patch.object(self.approval, "report_window_startup", side_effect=lambda status, **kw: events.append(status)), \
             patch.object(self.approval.messagebox, "showerror", side_effect=lambda *a: events.append("dialog")):
            self.assertEqual(self.approval.main(), 1)
        self.assertEqual(events, ["failed", "dialog"])

    def test_abort_failure_is_reported_before_the_modal_error(self):
        events = []
        with patch.object(self.abort, "AbortSessionWindow", side_effect=RuntimeError("startup failed")), \
             patch.object(self.abort.sys, "argv", ["abort_session_app.py", "fixture"]), \
             patch.object(self.abort, "report_window_startup", side_effect=lambda status, **kw: events.append(status)), \
             patch.object(self.abort.messagebox, "showerror", side_effect=lambda *a: events.append("dialog")):
            self.assertEqual(self.abort.main(), 1)
        self.assertEqual(events, ["failed", "dialog"])

    def test_managed_startup_failure_and_busy_window_exit_without_modal_dialogs(self):
        for failure, code in ((RuntimeError("fixture startup failure"), 1),
                              (self.approval.StateError("fixture owner changed"), 1),
                              (self.approval.ApprovalWindowBusyError("fixture busy"), 2)):
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(self.approval, "ApprovalWindow", side_effect=failure), \
                 patch.object(self.approval.sys, "argv", ["approval_app.py", "fixture"]), \
                 patch.object(self.approval, "managed_window_launch", return_value=True), \
                 patch.object(self.approval, "report_window_startup") as report, \
                 patch.object(self.approval, "messagebox") as dialogs:
                self.assertEqual(self.approval.main(), code)
                report.assert_called_once()
                dialogs.showerror.assert_not_called()
                dialogs.showinfo.assert_not_called()
        with patch.object(self.abort, "AbortSessionWindow", side_effect=RuntimeError("fixture failure")), \
             patch.object(self.abort.sys, "argv", ["abort_session_app.py", "fixture"]), \
             patch.object(self.abort, "managed_window_launch", return_value=True), \
             patch.object(self.abort, "report_window_startup") as report, \
             patch.object(self.abort, "messagebox") as dialogs:
            self.assertEqual(self.abort.main(), 1)
            report.assert_called_once_with("failed", error="fixture failure")
            dialogs.showerror.assert_not_called()

    def test_managed_queue_failure_and_busy_window_exit_without_modal_dialogs(self):
        for failure, status, code in ((RuntimeError("fixture queue failure"), "failed", 1),
                                      (self.approval.ApprovalWindowBusyError("fixture queue busy"), "already_open", 2)):
            with self.subTest(status=status), \
                 patch.object(self.approval, "ApprovalQueueWindow", side_effect=failure), \
                 patch.object(self.approval.sys, "argv", ["approval_app.py", "--queue", "fixture", "owner"]), \
                 patch.object(self.approval, "managed_window_launch", return_value=True), \
                 patch.object(self.approval, "report_window_startup") as report, \
                 patch.object(self.approval, "messagebox") as dialogs:
                self.assertEqual(self.approval.main(), code)
                report.assert_called_once_with(status, error=str(failure))
                dialogs.showerror.assert_not_called()
                dialogs.showinfo.assert_not_called()

    def test_pending_reopen_keeps_the_handling_window(self):
        service, root = Mock(), Mock()
        service.reopen_approval.return_value = {
            "approval_window_opened": False, "approval_window_starting": True}
        controller = SimpleNamespace(conversation_id="fixture-owner", owner_revision=0, root=root,
                                     _owned_session=Mock(return_value={"transaction_id": "fixture-tx"}))
        with patch.object(self.abort, "EditorService", return_value=service), \
             patch.object(self.abort, "messagebox") as dialogs:
            self.abort.AbortSessionWindow.reopen(controller)
            dialogs.showinfo.assert_called_once()
            dialogs.showerror.assert_not_called()
        root.destroy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
