"""Offline session-window workers: no Tk window, database, or live state."""
from __future__ import annotations

import importlib.util
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


def drain_operations(window, timeout=2):
    """Deliver worker results on the test's UI thread without a Tk mainloop."""
    deadline = time.monotonic() + timeout
    while window._busy:
        if time.monotonic() >= deadline:
            raise AssertionError("Session operation did not finish")
        window._worker.join(.01)
        window._poll_operation(window._operation_generation)


class AbortSessionResponsivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "scripts" / "abort_session_app.py"
        spec = importlib.util.spec_from_file_location("abort_session_responsiveness", path)
        cls.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.app)

    def setUp(self):
        self.main_thread = threading.get_ident()
        self.window = object.__new__(self.app.AbortSessionWindow)
        self.window.root = Mock()
        self.window.root.winfo_exists.return_value = True
        self.window.root.winfo_viewable.return_value = True
        self.window._initialize_operations()
        self.window.session_id = "fixture-session"
        self.window.conversation_id = "fixture-owner"
        self.window.owner_revision = 0
        self.window.state = Mock()
        self.window._owned_session = Mock(return_value={"state": "saved", "transaction_id": "fixture-tx"})
        self.window.operation_status = Mock()
        self.window._buttons = [Mock(), Mock()]
        self.dialogs = self.enterContext(patch.object(self.app, "messagebox"))
        for widget in [self.window.root, self.window.operation_status, *self.window._buttons]:
            for name in ["configure", "after", "destroy"]:
                getattr(widget, name).side_effect = self.assert_ui_thread
        for name in ["showinfo", "showerror", "askyesno"]:
            getattr(self.dialogs, name).side_effect = self.assert_ui_thread

    def assert_ui_thread(self, *args, **kwargs):
        self.assertEqual(threading.get_ident(), self.main_thread)

    def test_reopen_keeps_ui_responsive_prevents_duplicates_and_refuses_close_while_busy(self):
        entered, release = threading.Event(), threading.Event()
        service = Mock()
        def reopen(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), self.main_thread)
            entered.set()
            self.assertTrue(release.wait(2))
            return {"approval_window_starting": True}
        service.reopen_approval.side_effect = reopen
        self.addCleanup(release.set)
        with patch.object(self.app, "EditorService", return_value=service):
            started = time.monotonic()
            self.window.reopen()
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(entered.wait(.5))
            for button in self.window._buttons:
                button.configure.assert_called_with(state="disabled")
            self.window.reopen()
            self.window.recover()
            self.window.archive()
            self.window._close()
            self.window.root.destroy.assert_not_called()
            self.assertEqual(service.reopen_approval.call_count, 1)
            # The existing heartbeat can still run on the UI thread while the
            # worker is blocked, rather than waiting for the database/service.
            with patch.object(self.app, "report_window_heartbeat") as heartbeat, \
                    patch.object(self.app, "report_window_startup"):
                def mainloop():
                    self.window.root.after_idle.call_args.args[0]()
                    callbacks = [call.args[1] for call in self.window.root.after.call_args_list
                                 if call.args[0] == 1000]
                    self.assertEqual(len(callbacks), 1)
                    callbacks[0]()
                    self.assertTrue(self.window._busy)
                self.window.root.mainloop.side_effect = mainloop
                self.window.run()
                heartbeat.assert_called_once_with(phase="review")
            release.set()
            drain_operations(self.window)
        self.dialogs.showinfo.assert_called_once()
        self.window.root.destroy.assert_not_called()
        for button in self.window._buttons:
            button.configure.assert_called_with(state="normal")

    def test_unknown_or_unreleased_recovery_never_reports_completion_or_closes(self):
        for result in [
            {"outcome": "unknown", "session_released": False, "message": "身份尚未核实"},
            {"outcome": "not_applied", "session_released": False},
            {"outcome": "unexpected", "session_released": True},
        ]:
            with self.subTest(result=result), patch("gxp_editor.recovery.recover_session", return_value=result):
                self.window.recover()
                drain_operations(self.window)
                self.assertFalse(self.window._busy)
                self.dialogs.showinfo.assert_not_called()
                self.window.root.destroy.assert_not_called()
        self.assertEqual(self.dialogs.showerror.call_count, 3)
        self.assertIn("身份尚未核实", self.dialogs.showerror.call_args_list[0].args[1])

    def test_saved_result_finishes_on_worker_before_completion_notice(self):
        entered, release = threading.Event(), threading.Event()
        def finish():
            self.assertNotEqual(threading.get_ident(), self.main_thread)
            entered.set()
            self.assertTrue(release.wait(2))
        self.addCleanup(release.set)
        with patch("gxp_editor.recovery.recover_session", return_value={"outcome": "saved", "session_released": False}), \
                patch.object(self.window, "_finish", side_effect=finish):
            self.window.recover()
            self.assertTrue(entered.wait(.5))
            self.assertTrue(self.window._busy)
            self.dialogs.showinfo.assert_not_called()
            release.set()
            drain_operations(self.window)
        self.dialogs.showinfo.assert_called_once()
        self.window.root.destroy.assert_called_once()

    def test_changed_result_requires_main_thread_confirmation_then_rechecks(self):
        self.dialogs.askyesno.side_effect = lambda *args, **kwargs: (self.assert_ui_thread(), True)[1]
        with patch("gxp_editor.recovery.recover_session", side_effect=[
                {"outcome": "changed", "session_released": False},
                {"outcome": "closed_changed", "session_released": True},
        ]) as recover:
            self.window.recover()
            drain_operations(self.window)
        self.dialogs.askyesno.assert_called_once()
        self.assertNotIn("close_changed", recover.call_args_list[0].kwargs)
        self.assertTrue(recover.call_args_list[1].kwargs["close_changed"])
        self.assertEqual(self.window._owned_session.call_count, 2)
        self.window.root.destroy.assert_called_once()

    def test_declining_changed_result_keeps_session_and_window(self):
        self.dialogs.askyesno.side_effect = lambda *args, **kwargs: (self.assert_ui_thread(), False)[1]
        with patch("gxp_editor.recovery.recover_session", return_value={"outcome": "changed", "session_released": False}) as recover:
            self.window.recover()
            drain_operations(self.window)
        self.assertEqual(recover.call_count, 1)
        self.window.root.destroy.assert_not_called()
        self.dialogs.showinfo.assert_not_called()

    def test_archive_keeps_confirmation_and_snapshot_then_runs_in_worker(self):
        snapshot = {"state": "saved", "transaction_id": "fixture-tx"}
        self.window._owned_session.return_value = snapshot
        self.dialogs.askyesno.side_effect = lambda *args, **kwargs: (self.assert_ui_thread(), True)[1]
        def archive(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), self.main_thread)
            self.assertIs(kwargs["expected_snapshot"], snapshot)
            return {"session_released": True}
        with patch("gxp_editor.recovery.archive_edit_session", side_effect=archive) as archive_call:
            self.window.archive()
            drain_operations(self.window)
        self.dialogs.askyesno.assert_called_once()
        archive_call.assert_called_once()
        self.assertEqual(self.window._owned_session.call_count, 2)
        self.window.root.destroy.assert_called_once()

    def test_worker_failure_restores_controls_and_allows_close(self):
        service = Mock()
        service.reopen_approval.side_effect = RuntimeError("fixture unavailable")
        with patch.object(self.app, "EditorService", return_value=service):
            self.window.reopen()
            drain_operations(self.window)
        self.assertFalse(self.window._busy)
        self.dialogs.showerror.assert_called_once()
        self.assertIn("fixture unavailable", self.dialogs.showerror.call_args.args[1])
        self.window._close()
        self.window.root.destroy.assert_called_once()

    def test_archive_unreleased_result_keeps_window_and_reports_failure(self):
        self.dialogs.askyesno.side_effect = lambda *args, **kwargs: (self.assert_ui_thread(), True)[1]
        with patch("gxp_editor.recovery.archive_edit_session", return_value={"session_released": False}):
            self.window.archive()
            drain_operations(self.window)
        self.dialogs.showerror.assert_called_once()
        self.dialogs.showinfo.assert_not_called()
        self.window.root.destroy.assert_not_called()

    def test_thread_start_failure_restores_controls_without_running_operation(self):
        with patch.object(self.app.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("fixture thread unavailable")
            with patch.object(self.app, "EditorService") as service:
                self.window.reopen()
                self.window._poll_operation(self.window._operation_generation)
                service.assert_not_called()
        self.assertFalse(self.window._busy)
        self.dialogs.showerror.assert_called_once()
        for button in self.window._buttons:
            button.configure.assert_called_with(state="normal")

    def test_closed_window_does_not_start_another_operation(self):
        self.window._close()
        with patch.object(self.app, "EditorService") as service:
            self.window.reopen()
            service.assert_not_called()
        self.assertFalse(self.window._busy)


if __name__ == "__main__":
    unittest.main()
