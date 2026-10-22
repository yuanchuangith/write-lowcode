"""Exercise human-only approval and the real Qt worker lifecycle without a database."""
from __future__ import annotations

import copy
import importlib.util
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, QRect, QThread, Qt
from PySide6.QtTest import QTest

from tests.helpers import PLUGIN_ROOT, baseline, editor_config, relation, transaction
from gxp_editor.execution_lock import ApprovalWindowBusyError, approval_window_lock


class QtApprovalInteractionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        spec = importlib.util.spec_from_file_location("qt_approval_interactions", PLUGIN_ROOT / "scripts/approval_app.py")
        self.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app)
        self.artifact = transaction()
        self.state = Mock()
        self.path = Path(self.folder.name) / "approval.json"
        self.state.get_transaction.return_value = {"artifact_path": str(self.path), "state": "awaiting_approval", "session_id": self.artifact["session_id"]}
        self.session = {"id": self.artifact["session_id"], "action_name": "示例动作", "baseline": baseline(),
                        "conversation_id": "review", "owner_revision": 1, "transaction_id": "test", "state": "awaiting_approval"}
        self.state.get_session.return_value = self.session
        for target, value in (("load_config", editor_config()), ("StateStore", self.state), ("load_artifact", self.artifact),
                              ("save_artifact", None), ("execute_approved_transaction", {"state": "saved"}),
                              ("messagebox", Mock())):
            patcher = patch.object(self.app, target, return_value=value) if target != "messagebox" else patch.object(self.app, target, value)
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        secret = patch("gxp_editor.approval.approval_secret", return_value="test-secret")
        secret.start()
        self.addCleanup(secret.stop)
        self.window = self.app.ApprovalWindow("test", "review", 1)
        self.window.qt_app.setQuitOnLastWindowClosed(False)
        self.addCleanup(self.dispose_window, self.window)

    @staticmethod
    def dispose_window(window):
        window.close()
        window.root.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_rejected_approval_and_preview_never_sign_or_execute(self):
        original = copy.deepcopy(self.artifact)
        self.messagebox.askyesno.return_value = False
        self.window.approve_current()
        self.window.notebook.setCurrentIndex(1)
        self.window.copy_sql()
        self.window.navigate(1)
        self.assertEqual(self.artifact, original)
        self.save_artifact.assert_not_called()
        self.execute_approved_transaction.assert_not_called()

    def test_first_screen_does_not_wait_for_preview_metadata_and_result_updates_on_ui_thread(self):
        self.window.close()
        entered, release = threading.Event(), threading.Event()
        def metadata(config, expected):
            self.assertIsNot(QThread.currentThread(), self.window.qt_app.thread())
            entered.set()
            release.wait(2)
            return {"Test": {"key": "Test"}}, "metadata ready"
        self.addCleanup(release.set)
        with patch.object(self.app, "preview_registry_metadata", side_effect=metadata), \
             patch.object(self.app.ApprovalWindow, "load_preview_registry", side_effect=AssertionError("No synchronous metadata")):
            started = time.monotonic()
            self.window = self.app.ApprovalWindow("test", "review", 1)
            self.addCleanup(self.dispose_window, self.window)
            self.assertLess(time.monotonic() - started, 1)
            self.window.root.show()
            self.window.qt_app.processEvents()
            self.assertTrue(entered.wait(0.5))
            self.assertTrue(self.window.root.isVisible())
            self.assertFalse(self.window.component_review.registry)
            observed_threads = []
            original = self.window.component_review.registry_note.setText
            def record(text):
                observed_threads.append(QThread.currentThread())
                original(text)
            with patch.object(self.window.component_review.registry_note, "setText", side_effect=record):
                release.set()
                deadline = time.monotonic() + 1
                while not self.window.component_review.registry and time.monotonic() < deadline:
                    QTest.qWait(10)
            self.assertEqual(self.window.component_review.registry_note.text(), "metadata ready")
            self.assertTrue(observed_threads)
            self.assertTrue(all(thread is self.window.qt_app.thread() for thread in observed_threads))
        self.assertFalse(self.artifact["approvals"])
        self.execute_approved_transaction.assert_not_called()

    def test_small_window_keeps_action_buttons_visible_and_clickable(self):
        self.use_multiple_statements()
        self.messagebox.askyesno.return_value = False
        self.window.header.setText("一个很长的业务动作名称" * 12)
        for width, height in ((640, 420), (850, 620)):
            with self.subTest(width=width, height=height):
                self.window.root.resize(width, height)
                self.window.root.show()
                self.window.qt_app.processEvents()
                for button in (self.window.close_button, self.window.approve_button, self.window.execute_button):
                    rect = button.rect()
                    origin = button.mapTo(self.window.root, rect.topLeft())
                    self.assertGreaterEqual(origin.x(), 0)
                    self.assertGreaterEqual(origin.y(), 0)
                    self.assertLessEqual(origin.x() + button.width(), self.window.root.width())
                    self.assertLessEqual(origin.y() + button.height(), self.window.root.height())
                    center = button.mapTo(self.window.root, rect.center())
                    self.assertIs(self.window.root.childAt(center), button)
                self.messagebox.askyesno.reset_mock()
                QTest.mouseClick(self.window.approve_button, Qt.MouseButton.LeftButton)
                self.messagebox.askyesno.assert_called_once()
        self.save_artifact.assert_not_called()
        self.execute_approved_transaction.assert_not_called()

    def test_initial_window_fits_scaled_desktop_and_taskbar_work_area(self):
        # 1024x768 at 125% scaling gives about 819x614 logical pixels.
        available = QRect(20, 30, 819, 614)
        screen = Mock()
        screen.availableGeometry.return_value = available
        with patch.object(self.window.root, "screen", return_value=screen):
            self.window.root.fit_to_screen()
            self.window.root.show()
            self.window.qt_app.processEvents()
            self.assertTrue(available.contains(self.window.root.frameGeometry()))
            self.assertFalse(self.window.root.isMaximized())

    def test_human_approval_enables_execution_but_does_not_execute(self):
        self.messagebox.askyesno.return_value = True
        self.window.approve_current()
        self.assertEqual(len(self.artifact["approvals"]), 1)
        self.save_artifact.assert_called_once()
        self.execute_approved_transaction.assert_not_called()
        self.assertTrue(self.window.execute_button.isEnabled())
        self.assertFalse(self.window.approve_button.isEnabled())

    def use_multiple_statements(self):
        self.artifact.clear()
        self.artifact.update(transaction(current_relations=[relation()], desired_relations={"new-node": "{}"}))
        self.window.render()
        self.assertEqual(len(self.artifact["statements"]), 3)

    def test_each_click_approves_one_row_and_continues_in_same_window(self):
        self.use_multiple_statements()
        self.messagebox.askyesno.return_value = True
        shell = self.window.root
        shell.show()
        self.window.qt_app.processEvents()
        statements = self.artifact["statements"]
        self.assertTrue(all(item["expected_rows"] == 1 for item in statements))
        for index, statement in enumerate(statements):
            with self.subTest(index=index):
                self.assertEqual(self.window.index, index)
                self.assertIn("批准最后一条" if index == 2 else "继续下一条", self.window.approve_button.text())
                QTest.mouseClick(self.window.approve_button, Qt.MouseButton.LeftButton)
                self.assertEqual(set(self.artifact["approvals"]), {item["id"] for item in statements[:index + 1]})
                self.assertEqual(self.save_artifact.call_count, index + 1)
                self.assertIs(self.window.root, shell)
                self.assertTrue(shell.isVisible())
                self.assertEqual(self.window.statement_list.currentRow(), min(index + 1, 2))
                self.assertEqual(self.window.execute_button.isEnabled(), index == 2)
                self.execute_approved_transaction.assert_not_called()
        self.assertFalse(self.window.approve_button.isEnabled())
        self.assertIn("请确认保存草稿", self.window.progress.text())
        self.messagebox.askyesno.return_value = False
        self.window.execute()
        self.execute_approved_transaction.assert_not_called()
        self.assertTrue(shell.isVisible())

    def test_continuation_skips_current_approvals(self):
        self.use_multiple_statements()
        middle = self.artifact["statements"][1]
        approved = copy.deepcopy(self.app.approve_statement(editor_config(), self.artifact, middle["id"]))
        self.window.render()
        self.messagebox.askyesno.return_value = True
        self.window.approve_current()
        self.assertEqual(self.window.index, 2)
        self.assertEqual(self.artifact["approvals"][middle["id"]], approved)
        self.assertEqual(len(self.artifact["approvals"]), 2)
        self.assertIn("批准最后一条", self.window.approve_button.text())
        self.execute_approved_transaction.assert_not_called()

    def test_continuation_returns_to_an_earlier_expired_approval(self):
        self.use_multiple_statements()
        statements = self.artifact["statements"]
        for statement in statements[:2]:
            self.app.approve_statement(editor_config(), self.artifact, statement["id"])
        self.window.select_statement(2)
        self.artifact["approvals"][statements[0]["id"]]["expires_at"] = "2000-01-01T00:00:00"
        self.window.refresh_approval_state()
        self.messagebox.askyesno.return_value = True
        self.window.approve_current()
        self.assertEqual(self.window.index, 0)
        self.assertEqual(self.window.statement_list.currentRow(), 0)
        self.assertFalse(self.window.execute_button.isEnabled())
        self.window.approve_current()
        self.assertTrue(self.window.execute_button.isEnabled())
        self.assertEqual(self.save_artifact.call_count, 2)
        self.execute_approved_transaction.assert_not_called()

    def test_cancelled_approval_does_not_advance_or_sign_next_item(self):
        self.use_multiple_statements()
        self.messagebox.askyesno.return_value = False
        original = copy.deepcopy(self.artifact)
        self.window.approve_current()
        self.assertEqual(self.window.index, 0)
        self.assertEqual(self.artifact, original)
        self.save_artifact.assert_not_called()
        self.execute_approved_transaction.assert_not_called()

    def test_reopening_resumes_at_pending_or_expired_statement(self):
        self.use_multiple_statements()
        statements = self.artifact["statements"]
        for index in (0, 2):
            self.app.approve_statement(editor_config(), self.artifact, statements[index]["id"])
        for expected_index in (1, 0):
            with self.subTest(expected_index=expected_index):
                if expected_index == 0:
                    self.artifact["approvals"][statements[0]["id"]]["expires_at"] = "2000-01-01T00:00:00"
                self.window.close()
                original = copy.deepcopy(self.artifact)
                self.window = self.app.ApprovalWindow("test", "review", 1)
                self.addCleanup(self.dispose_window, self.window)
                self.assertEqual(self.window.index, expected_index)
                self.assertEqual(self.window.statement_list.currentRow(), expected_index)
                self.assertEqual(self.artifact, original)
        self.save_artifact.assert_not_called()
        self.execute_approved_transaction.assert_not_called()

    def test_transfer_while_confirmation_is_open_blocks_signature(self):
        def transfer(*args, **kwargs):
            self.session["owner_revision"] += 1
            return True
        self.messagebox.askyesno.side_effect = transfer
        self.window.approve_current()
        self.assertFalse(self.artifact.get("approvals"))
        self.save_artifact.assert_not_called()
        self.assertFalse(self.window.execute_button.isEnabled())
        self.assertFalse(self.window.approve_button.isEnabled())

    def test_execute_requires_all_approvals_even_when_called_directly(self):
        self.window.execute()
        self.execute_approved_transaction.assert_not_called()
        self.messagebox.showerror.assert_called_once()

    def test_unverified_worker_result_keeps_window_for_recovery(self):
        self.execute_approved_transaction.return_value = {
            "state": "saved", "verified": False, "session_released": False, "verification_error": "read failed",
        }
        self.messagebox.askyesno.return_value = True
        self.window.approve_current()
        self.window.root.show()
        self.window.execute()
        deadline = time.monotonic() + 3
        while self.window._executing and time.monotonic() < deadline:
            QTest.qWait(10)
        self.window.worker.wait(1000)
        self.assertTrue(self.window._review_blocked)
        self.assertTrue(self.window.root.isVisible())
        self.messagebox.showinfo.assert_not_called()
        self.messagebox.showerror.assert_called_once()
        with self.assertRaises(ApprovalWindowBusyError):
            with approval_window_lock(self.path):
                self.fail("Unverified save was released by the window")

    def test_worker_keeps_window_locked_and_delivers_result_on_gui_thread(self):
        release = threading.Event()
        started = threading.Event()
        def execute(_):
            started.set()
            release.wait(3)
            return {"state": "saved", "verified": True, "session_released": True, "runtime_finalized": False}
        self.execute_approved_transaction.side_effect = execute
        result_threads = []
        self.messagebox.showinfo.side_effect = lambda *args, **kwargs: result_threads.append(QThread.currentThread())
        self.messagebox.askyesno.return_value = True
        self.window.approve_current()
        self.window.root.show()
        self.window.execute()
        try:
            self.assertTrue(started.wait(1))
            self.assertFalse(self.window.execute_button.isEnabled())
            self.window.root.close()
            self.assertTrue(self.window.root.isVisible())
            with self.assertRaises(ApprovalWindowBusyError):
                with approval_window_lock(self.path):
                    self.fail("Executing window released its lock")
        finally:
            release.set()
            deadline = time.monotonic() + 3
            while self.window._executing and time.monotonic() < deadline:
                QTest.qWait(10)
            self.window.worker.wait(1000)
        self.assertFalse(self.window._executing)
        self.assertEqual(result_threads, [self.window.qt_app.thread()])
        self.assertFalse(self.window.root.isVisible())
        with approval_window_lock(self.path):
            pass

    def test_component_preview_preserves_nested_nodes_and_full_parameters(self):
        from gxp_editor.qt_review_ui import ComponentReview
        before = {"actionData": [{"key": "main", "title": "主动作", "data": [
            {"key": "condition", "title": "条件", "elementKey": "IfCondition", "paramsValue": {}},
            {"key": "set", "title": "赋值", "elementKey": "SetVariable", "depth": ["condition"],
             "paramsValue": {"inputParams": {"value": "原值"}}},
        ]}], "globalVariables": []}
        after = copy.deepcopy(before)
        after["actionData"][0]["data"][1]["paramsValue"]["inputParams"]["value"] = "新值" * 500
        original = copy.deepcopy(after)
        preview = ComponentReview()
        preview.render(before, after)
        entry = preview.lookup["after"][("main", "set")]
        self.assertEqual(entry.parent().text(0), "条件")
        self.assertEqual(preview.selected_change["kind"], "修改")
        preview.open_details()
        detail = preview.dialogs[0]
        texts = detail.findChildren(self.app.QPlainTextEdit)
        self.assertTrue(any("新值" * 500 in view.toPlainText() for view in texts))
        self.assertEqual(after, original)
        detail.close()
        preview.close()
        preview.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_failed_no_write_releases_window_but_uncertain_outcome_stays_blocked(self):
        for outcome in ("failed_no_write", "failed"):
            with self.subTest(outcome=outcome):
                if outcome == "failed":
                    self.state.get_transaction.return_value["state"] = "awaiting_approval"
                    self.window = self.app.ApprovalWindow("test", "review", 1)
                    self.addCleanup(self.dispose_window, self.window)
                self.window.root.show()
                self.window.worker = Mock()
                self.window._executing = True
                self.state.get_transaction.return_value["state"] = outcome
                self.window._execution_failure("模拟连接错误")
                self.assertFalse(self.window._executing)
                self.assertFalse(self.window.execute_button.isEnabled())
                self.assertEqual(self.window.root.isVisible(), outcome == "failed")
                if outcome == "failed":
                    with self.assertRaises(ApprovalWindowBusyError):
                        with approval_window_lock(self.path):
                            self.fail("Uncertain outcome released its review window")
                else:
                    with approval_window_lock(self.path):
                        pass

    def test_history_preview_has_no_approval_or_execution_path(self):
        spec = importlib.util.spec_from_file_location("qt_history_preview", PLUGIN_ROOT / "scripts/preview_saved_approval.py")
        viewer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(viewer)
        original = copy.deepcopy(self.artifact)
        window = viewer.SavedApprovalPreview({"id": "history", "session_id": "session-1", "action_name": "历史动作"}, self.artifact, baseline())
        try:
            window.approve_current()
            window.execute()
            window.navigate(1)
            self.assertFalse(window.approve_button.isEnabled())
            self.assertFalse(window.execute_button.isEnabled())
            self.assertEqual(self.artifact, original)
        finally:
            self.dispose_window(window)


if __name__ == "__main__":
    unittest.main()
