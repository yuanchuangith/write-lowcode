from __future__ import annotations

import copy
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import MCP_ROOT, PLUGIN_ROOT, baseline, transaction
from gxp_editor.execution_lock import ApprovalWindowBusyError, approval_window_lock, execution_lock
from gxp_editor.state import StateError
from gxp_editor.transaction import TransactionPlanError


class ApprovalWindowLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "first.json"

    def test_one_window_per_transaction_and_execution_is_independent(self):
        with approval_window_lock(self.path), approval_window_lock(self.directory / "second.json"):
            with self.assertRaisesRegex(ApprovalWindowBusyError, "已有窗口打开"):
                with approval_window_lock(self.path):
                    self.fail("A second window acquired the same transaction")
            with execution_lock(self.path):
                pass
        with approval_window_lock(self.path):
            pass

    def test_lock_is_shared_across_processes_and_released_on_process_exit(self):
        ready = self.directory / "ready.txt"
        code = (
            "import os, sys; from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); "
            "from gxp_editor.execution_lock import approval_window_lock;\n"
            "with approval_window_lock(sys.argv[2]):\n"
            " Path(sys.argv[3]).write_text(str(os.getpid()), encoding='ascii')\n"
            " sys.stdin.read()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(MCP_ROOT), str(self.path), str(ready)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "Child did not acquire the window lock within 5 seconds")
            with self.assertRaises(ApprovalWindowBusyError):
                with approval_window_lock(self.path):
                    self.fail("Second process opened the same approval")
            with approval_window_lock(self.directory / "other.json"):
                pass
            os.kill(int(ready.read_text(encoding="ascii")), signal.SIGTERM)
            process.wait(timeout=5)
            with approval_window_lock(self.path):
                pass
        finally:
            process.stdin.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()


class ApprovalWindowLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        spec = importlib.util.spec_from_file_location("approval_window_lifecycle", PLUGIN_ROOT / "scripts" / "approval_app.py")
        self.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app)
        self.qt_app = self.app.application()
        self.qt_app.setQuitOnLastWindowClosed(False)
        self.artifact = transaction()
        self.original = copy.deepcopy(self.artifact)
        self.state = Mock()
        def get_transaction(tx):
            self.state.get_session.return_value = {
                "id": "session-123456", "action_name": "测试动作", "baseline": baseline(),
                "conversation_id": "对话一", "owner_revision": 2,
                "transaction_id": tx, "state": "awaiting_approval",
            }
            return {"artifact_path": str(self.directory / f"{tx}.json"), "state": "awaiting_approval"}
        self.state.get_transaction.side_effect = get_transaction
        for target, options in (
            ("load_config", {}), ("StateStore", {"return_value": self.state}),
            ("load_artifact", {"return_value": self.artifact}),
            ("approve_statement", {}), ("save_artifact", {}), ("execute_approved_transaction", {}),
        ):
            patcher = patch.object(self.app, target, **options)
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)

    def window(self, tx):
        window = self.app.ApprovalWindow(tx, "对话一", 2)
        self.addCleanup(window.close)
        return window

    def test_duplicate_main_has_clear_message_and_distinct_windows_can_open(self):
        first = self.window("first")
        self.assertIn("对话 对话一", first.root.windowTitle())
        self.assertIn("session-", first.root.windowTitle())
        with patch.object(self.app.sys, "argv", ["approval_app.py", "first", "对话一", "2"]), \
             patch.object(self.app.messagebox, "showinfo") as alert:
            self.assertEqual(self.app.main(), 2)
            self.assertIn("已有窗口打开", alert.call_args.args[1])
        second = self.window("second")
        first.root.close()
        reopened = self.window("first")
        reopened.close()
        with self.assertRaises(ApprovalWindowBusyError):
            with approval_window_lock(self.directory / "second.json"):
                self.fail("Closing the first window released the second window")
        second.close()
        self.assertEqual(self.artifact, self.original)
        self.approve_statement.assert_not_called()
        self.save_artifact.assert_not_called()
        self.execute_approved_transaction.assert_not_called()

    def test_initialization_error_releases_window_lock(self):
        with patch.object(self.app, "validate_transaction_artifact", side_effect=TransactionPlanError("invalid")), \
             patch.object(self.app, "ApprovalShell") as shell:
            with self.assertRaisesRegex(TransactionPlanError, "invalid"):
                self.window("first")
            shell.assert_not_called()
        with approval_window_lock(self.directory / "first.json"):
            pass

    def test_stale_owner_and_transfer_round_trip_are_rejected_before_ui(self):
        for conversation, revision in (("旧对话", 2), ("对话一", 1), ("", None)):
            with self.subTest(conversation=conversation, revision=revision), patch.object(self.app, "ApprovalShell") as shell:
                with self.assertRaisesRegex(StateError, "已转交"):
                    self.app.ApprovalWindow("first", conversation, revision)
                shell.assert_not_called()
            with approval_window_lock(self.directory / "first.json"):
                pass
        self.save_artifact.assert_not_called()

    def test_closed_transaction_does_not_open_a_dead_approval_window(self):
        self.state.get_transaction.side_effect = None
        self.state.get_transaction.return_value = {"artifact_path": str(self.directory / "closed.json"), "state": "closed_unverified"}
        with patch.object(self.app, "ApprovalShell") as shell:
            with self.assertRaises(StateError):
                self.window("closed")
            shell.assert_not_called()
        with approval_window_lock(self.directory / "closed.json"):
            pass

    def test_startup_failure_has_visible_session_handling_guidance(self):
        with patch.object(self.app, "ApprovalWindow", side_effect=FileNotFoundError("missing approval")), \
             patch.object(self.app.sys, "argv", ["approval_app.py", "missing"]), \
             patch.object(self.app.messagebox, "showerror") as error:
            self.assertEqual(self.app.main(), 1)
            self.assertIn("abort_edit_session", error.call_args.args[1])



if __name__ == "__main__":
    unittest.main()
