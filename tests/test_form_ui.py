"""Human-only form approval interactions against the real offscreen Qt window."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QThread, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QMessageBox, QPushButton

from gxp_form_editor.ui import run_review_window


class FakeReviewService:
    def __init__(self, state="pending", approval_valid=False):
        self.state = state
        self.approval_valid = approval_valid
        self.reads = []
        self.approvals = []
        self.executions = []
        self.cancellations = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.worker_thread = None

    def get_form_review(self, write_id, conversation_id):
        self.reads.append((write_id, conversation_id))
        return {"write": {"id": write_id, "state": self.state}, "session": {"id": "form-session"},
                "approval_valid": self.approval_valid, "target": {"page_id": "test-page"},
                "changes": [{"component_id": "field", "path": "/required", "old_value": False, "value": True}],
                "requests": [{"method": "PUT", "path": "/native-form-save"}]}

    def approve_form_write(self, write_id, conversation_id, reviewer):
        self.approvals.append((write_id, conversation_id, reviewer))
        self.state = "approved"
        self.approval_valid = True
        return {"write_id": write_id, "state": self.state}

    def execute_form_write(self, write_id, conversation_id):
        self.executions.append((write_id, conversation_id))
        self.worker_thread = threading.current_thread()
        self.entered.set()
        if not self.release.wait(3):
            raise AssertionError("offscreen test did not release fake save")
        self.state = "saved"
        return {"write_id": write_id, "state": "saved", "verified": True, "session_released": True}

    def cancel_form_edit_session(self, session_id):
        self.cancellations.append(session_id)
        self.state = "cancelled"
        return {"session_id": session_id, "state": self.state}


class FormApprovalUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    @staticmethod
    def button(window, text):
        return next(button for button in window.findChildren(QPushButton) if button.text() == text)

    @staticmethod
    def click(widget):
        if isinstance(widget, QCheckBox):
            QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(8, widget.height() // 2))
        else:
            QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
        QApplication.processEvents()

    def exercise(self, service, scenario):
        window = None

        def interact():
            nonlocal window
            QApplication.processEvents()
            window = next(widget for widget in self.app.topLevelWidgets()
                          if isinstance(widget, QDialog) and widget.windowTitle() == "GXP 表单配置编辑器 · 人工审批"
                          and widget.isVisible())
            scenario(window)
            return 0

        try:
            with patch.object(QApplication, "exec", side_effect=interact):
                self.assertEqual(run_review_window("form-write", "form-owner", service=service), 0)
        finally:
            service.release.set()
            if window is not None:
                for worker in window.findChildren(QThread):
                    self.assertTrue(worker.wait(4000), "Qt worker did not finish before cleanup")
                QApplication.processEvents()
                window.close()
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_open_and_refresh_never_approve_or_execute(self):
        service = FakeReviewService()

        def scenario(window):
            self.assertEqual(service.reads, [("form-write", "form-owner")])
            self.assertFalse(self.button(window, "人工批准").isEnabled())
            self.assertFalse(self.button(window, "执行已批准保存…").isEnabled())
            self.click(self.button(window, "刷新核对"))
            self.assertEqual(len(service.reads), 2)
            self.assertEqual(service.approvals, [])
            self.assertEqual(service.executions, [])

        self.exercise(service, scenario)

    def test_only_checked_human_approve_click_signs_and_does_not_execute(self):
        service = FakeReviewService()

        def scenario(window):
            approve = self.button(window, "人工批准")
            self.click(approve)
            self.assertEqual(service.approvals, [])
            checkbox = window.findChild(QCheckBox)
            self.assertFalse(checkbox.isChecked())
            self.click(checkbox)
            self.assertTrue(approve.isEnabled())
            self.assertEqual(service.approvals, [])
            self.click(approve)
            self.assertEqual(len(service.approvals), 1)
            self.assertEqual(service.approvals[0][:2], ("form-write", "form-owner"))
            self.assertTrue(service.approvals[0][2])
            self.assertFalse(approve.isEnabled())
            self.assertTrue(self.button(window, "执行已批准保存…").isEnabled())
            self.assertEqual(service.executions, [])

        self.exercise(service, scenario)

    def test_save_confirmation_defaults_to_no_and_no_never_sends_save(self):
        service = FakeReviewService(state="approved", approval_valid=True)

        def scenario(window):
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question:
                self.click(self.button(window, "执行已批准保存…"))
            question.assert_called_once()
            self.assertEqual(question.call_args.args[-1], QMessageBox.StandardButton.No)
            self.assertEqual(service.executions, [])
            self.assertEqual(service.approvals, [])
            self.assertTrue(window.isVisible())

        self.exercise(service, scenario)

    def test_reopen_verified_approval_can_resume_without_new_approval(self):
        service = FakeReviewService(state="approved", approval_valid=True)

        def scenario(window):
            self.assertFalse(self.button(window, "人工批准").isEnabled())
            self.assertTrue(self.button(window, "执行已批准保存…").isEnabled())
            self.click(self.button(window, "刷新核对"))
            self.assertTrue(self.button(window, "执行已批准保存…").isEnabled())
            self.assertEqual(service.approvals, [])
            self.assertEqual(service.executions, [])

        self.exercise(service, scenario)

    def test_approved_state_without_verified_signature_cannot_execute(self):
        service = FakeReviewService(state="approved", approval_valid=False)

        def scenario(window):
            self.assertFalse(self.button(window, "执行已批准保存…").isEnabled())
            self.assertFalse(self.button(window, "人工批准").isEnabled())
            self.click(self.button(window, "执行已批准保存…"))
            self.assertEqual(service.approvals, [])
            self.assertEqual(service.executions, [])

        self.exercise(service, scenario)

    def test_background_save_blocks_close_reject_refresh_and_duplicate_execution(self):
        service = FakeReviewService(state="approved", approval_valid=True)
        service.release.clear()

        def scenario(window):
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                self.click(self.button(window, "执行已批准保存…"))
            deadline = time.monotonic() + 1
            while not service.entered.is_set() and time.monotonic() < deadline:
                QTest.qWait(5)
            self.assertTrue(service.entered.is_set())
            self.assertIsNot(service.worker_thread, threading.current_thread())
            self.assertFalse(self.button(window, "关闭").isEnabled())
            self.assertFalse(self.button(window, "刷新核对").isEnabled())
            self.assertFalse(self.button(window, "执行已批准保存…").isEnabled())
            self.assertFalse(window.close())
            window.reject()
            QApplication.processEvents()
            self.assertTrue(window.isVisible())
            self.click(self.button(window, "执行已批准保存…"))
            self.assertEqual(service.executions, [("form-write", "form-owner")])
            service.release.set()
            for worker in window.findChildren(QThread):
                self.assertTrue(worker.wait(2000))
            QApplication.processEvents()
            self.assertTrue(self.button(window, "关闭").isEnabled())
            self.assertFalse(self.button(window, "执行已批准保存…").isEnabled())
            self.assertEqual(service.approvals, [])

        self.exercise(service, scenario)


if __name__ == "__main__":
    unittest.main()
