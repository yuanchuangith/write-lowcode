"""Use real SQLite/artifacts/Qt workers; all execution stays offline."""
from __future__ import annotations

import copy
import importlib.util
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest

from tests.helpers import PLUGIN_ROOT, baseline, editor_config, transaction
from gxp_editor.approval import load_artifact, save_artifact, transaction_binding_hash, verify_approvals
from gxp_editor.execution_lock import ApprovalWindowBusyError, approval_window_lock
from gxp_editor.state import StateStore
from gxp_editor.service import EditorService
from gxp_editor.logic_review import plan_binding_hash


class QtApprovalQueueTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.state = StateStore(self.root / "state.sqlite3")
        self.owner = self.state.create_conversation()["conversation_id"]
        self.queue = self.state.create_edit_queue(self.owner)
        self.config = editor_config()
        spec = importlib.util.spec_from_file_location("approval_queue_interactions", PLUGIN_ROOT / "scripts/approval_app.py")
        self.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app)
        for target, kwargs in (
            ("gxp_editor.config.runtime_root", {"return_value": self.root}),
            ("gxp_editor.approval.approval_secret", {"return_value": "offline-test-secret"}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, kwargs in (("load_config", {"return_value": self.config}), ("StateStore", {"return_value": self.state}),
                               ("messagebox", {"new": Mock()}), ("execute_approved_transaction", {"side_effect": self.save_offline})):
            patcher = patch.object(self.app, target, **kwargs)
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        self.first = self.add_action(1)
        self.second = self.add_action(2)
        self.window = self.open_window()
        self.messagebox.askyesno.return_value = True

    def add_action(self, number):
        draft = baseline(id=f"design-{number}", ref_id=f"ref-{number}", code=f"Action{number}", metadata={
            "form_name": f"表单{number}", "action_name": f"动作{number}", "action_code": f"Action{number}"})
        session = self.state.create_session(draft, "backup", "compiler", conversation_id=self.owner, queue_id=self.queue)
        plan = self.state.create_plan(session["id"], "plan.json", "old", "new", "compiler", conversation_id=self.owner)
        path = self.root / f"transaction-{number}.json"
        tx = self.state.create_transaction(session["id"], plan, "write", str(path), conversation_id=self.owner)
        artifact = transaction(draft=draft)
        artifact.update(transaction_id=tx, session_id=session["id"], plan_id=plan)
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        save_artifact(path, artifact)
        return tx

    def add_planned_action(self, number):
        draft = baseline(id=f"planned-design-{number}", ref_id=f"planned-ref-{number}", code=f"Planned{number}", metadata={
            "form_name": f"表单{number}", "action_name": f"动作{number}", "action_code": f"Planned{number}"})
        session = self.state.create_session(draft, "backup", "compiler", conversation_id=self.owner, queue_id=self.queue)
        desired = transaction(draft=draft)["desired_design"]
        envelope = {"session_id": session["id"], "base_content_hash": draft["content_hash"], "desired": desired,
                    "compiler_binding": {}, "hashes": {}, "business_review": {}}
        path = self.root / f"planned-{number}.json"
        save_artifact(path, envelope)
        plan = self.state.create_plan(session["id"], str(path), draft["content_hash"], plan_binding_hash(envelope),
                                      "compiler", conversation_id=self.owner)
        return session["id"], plan

    def choose_queue_row(self, index):
        self.window.queue_choices.showPopup()
        self.window.qt_app.processEvents()
        view = self.window.queue_choices.view()
        model_index = view.model().index(index, 0)
        view.scrollTo(model_index)
        self.window.qt_app.processEvents()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(model_index).center())
        self.window.qt_app.processEvents()

    def prepare_planned_action(self, selected, plan):
        draft = self.state.get_session(selected)["baseline"]
        path = self.root / f"prepared-{selected}.json"
        tx = self.state.create_transaction(selected, plan, "write", str(path), conversation_id=self.owner)
        artifact = transaction(draft=draft)
        artifact.update(transaction_id=tx, session_id=selected, plan_id=plan)
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        save_artifact(path, artifact)
        return tx

    def open_window(self):
        window = self.app.ApprovalQueueWindow(self.queue, self.owner)
        window.qt_app.setQuitOnLastWindowClosed(False)
        window.root.show()
        window.qt_app.processEvents()
        self.addCleanup(self.dispose, window)
        return window

    @staticmethod
    def dispose(window):
        window.close()
        window.root.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def artifact(self, tx):
        return load_artifact(self.state.get_transaction(tx)["artifact_path"])

    def save_offline(self, tx):
        artifact = self.artifact(tx)
        verify_approvals(self.config, artifact)
        self.state.claim_transaction(tx)
        self.state.update_transaction(tx, "saved")
        self.state.update_session(artifact["session_id"], "saved", conversation_id=self.owner)
        self.state.finish_verified_save(
            tx, content_hash=artifact["new_content_hash"], relation_hash=artifact["expected_relation_hash"],
            conversation_id=self.owner, owner_revision=0,
        )
        return {"state": "saved", "verified": True, "session_released": True, "runtime_finalized": False}

    def save_current(self):
        self.window.approve_current()
        self.window.execute()
        deadline = time.monotonic() + 3
        while self.window._executing and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertFalse(self.window._executing)

    def test_save_advances_to_next_form_in_same_shell_without_carrying_approval(self):
        shell = self.window.root
        self.assertEqual(self.window.transaction_id, self.first)
        self.window.approve_current()
        self.assertEqual(self.window.transaction_id, self.first)
        self.execute_approved_transaction.assert_not_called()
        self.assertFalse(self.artifact(self.second)["approvals"])
        self.window.execute()
        deadline = time.monotonic() + 3
        while self.window._executing and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertIs(self.window.root, shell)
        self.assertTrue(shell.isVisible())
        self.assertEqual(self.window.transaction_id, self.second)
        self.assertIn("表单2", shell.windowTitle())
        self.assertIn("动作2", shell.windowTitle())
        self.assertIn("已保存 1/2", self.window.queue_progress.text())
        self.assertFalse(self.window.execute_button.isEnabled())
        self.assertFalse(self.artifact(self.second)["approvals"])
        self.execute_approved_transaction.assert_called_once_with(self.first)
        self.assertEqual(self.state.get_session(self.state.get_transaction(self.first)["session_id"])["state"], "finished")

    def test_unverified_save_stops_queue_before_next_approval(self):
        def save_without_verification(tx):
            self.state.claim_transaction(tx)
            self.state.update_transaction(tx, "saved")
            self.state.update_session(self.state.get_transaction(tx)["session_id"], "saved", conversation_id=self.owner)
            return {"state": "saved", "verified": False, "session_released": False}
        self.execute_approved_transaction.side_effect = save_without_verification
        self.save_current()
        self.assertTrue(self.window._queue_stopped)
        self.assertEqual(self.window.transaction_id, self.first)
        self.assertFalse(self.artifact(self.second)["approvals"])
        self.window.close()
        self.window = self.open_window()
        self.assertTrue(self.window._queue_stopped)
        self.assertFalse(self.artifact(self.second)["approvals"])

    def test_empty_queue_stays_open_and_picks_up_later_preparation(self):
        self.save_current()
        self.save_current()
        shell = self.window.root
        self.assertFalse(self.window._active_review)
        self.assertTrue(shell.isVisible())
        self.assertIn("已保存 2/2", self.window.queue_progress.text())
        waiting_page = shell.centralWidget()
        self.window.refresh_approval_state()
        self.assertIs(shell.centralWidget(), waiting_page)
        third = self.add_action(3)
        self.window.refresh_approval_state()
        self.assertIs(self.window.root, shell)
        self.assertTrue(self.window._active_review)
        self.assertEqual(self.window.transaction_id, third)
        self.assertFalse(self.artifact(third)["approvals"])
        self.assertEqual(self.execute_approved_transaction.call_count, 2)

    def test_publication_list_accumulates_and_survives_reopening(self):
        self.assertNotIn("重新发布以下动作", self.window.queue_result.text())
        self.save_current()
        self.assertIn("动作1（Action1） · 表单1", self.window.queue_result.text())
        self.assertNotIn("Action2", self.window.queue_result.text())
        self.save_current()
        self.assertIn("动作1（Action1） · 表单1", self.window.queue_result.text())
        self.assertIn("动作2（Action2） · 表单2", self.window.queue_result.text())
        self.window.close()
        self.window = self.open_window()
        self.assertIn("动作1（Action1） · 表单1", self.window.queue_result.text())
        self.assertIn("动作2（Action2） · 表单2", self.window.queue_result.text())
        service = object.__new__(EditorService)
        service._conversation_id = self.owner
        service.state = self.state
        targets = service.get_edit_queue(self.queue)["publication_targets"]
        self.assertEqual([target["action_code"] for target in targets], ["Action1", "Action2"])

    def test_later_unverified_save_keeps_only_previous_publication_target(self):
        self.save_current()
        def save_without_verification(tx):
            self.state.claim_transaction(tx)
            self.state.update_transaction(tx, "saved")
            self.state.update_session(self.state.get_transaction(tx)["session_id"], "saved", conversation_id=self.owner)
            return {"state": "saved", "verified": False, "session_released": False}
        self.execute_approved_transaction.side_effect = save_without_verification
        self.save_current()
        self.assertTrue(self.window._queue_stopped)
        self.assertIn("动作1（Action1）", self.window.queue_result.text())
        self.assertNotIn("Action2", self.window.queue_result.text())
        self.window.close()
        self.window = self.open_window()
        self.assertTrue(self.window._queue_stopped)
        self.assertIn("动作1（Action1）", self.window.queue_result.text())
        self.assertNotIn("Action2", self.window.queue_result.text())

    def test_navigation_and_reopen_only_show_pending_without_signing(self):
        original = copy.deepcopy(self.artifact(self.second))
        self.window._select_queue_item(1)
        self.assertEqual(self.window.transaction_id, self.second)
        self.assertEqual(self.artifact(self.second), original)
        self.window._select_queue_item(0)
        self.window.approve_current()
        signed = copy.deepcopy(self.artifact(self.first))
        self.window.close()
        self.window = self.open_window()
        self.assertEqual(self.window.transaction_id, self.first)
        self.assertEqual(self.artifact(self.first), signed)
        self.assertTrue(self.window.execute_button.isEnabled())
        self.execute_approved_transaction.assert_not_called()

    def test_saved_and_planned_rows_can_be_clicked_without_signing_or_executing(self):
        self.save_current()
        planned_session, _ = self.add_planned_action(3)
        self.window.refresh_approval_state()
        unchanged_second = copy.deepcopy(self.artifact(self.second))
        saved_first = copy.deepcopy(self.artifact(self.first))
        self.choose_queue_row(0)
        self.assertEqual(self.window.queue_choices.currentData(), saved_first["session_id"])
        self.assertIn("已保存", self.window.badge.text())
        self.assertGreaterEqual(self.window.queue_details.count(), 2)
        self.assertFalse(self.window.approve_button.isEnabled())
        self.assertFalse(self.window.execute_button.isEnabled())
        self.window.approve_current()
        self.window.execute()
        self.choose_queue_row(2)
        self.assertEqual(self.window.queue_choices.currentData(), planned_session)
        self.assertIn("尚未提交审批", self.window.progress.text())
        self.assertGreaterEqual(self.window.queue_details.count(), 2)
        self.assertFalse(self.window._active_review)
        self.window.approve_current()
        self.window.execute()
        self.assertEqual(self.state.session_transactions(planned_session), [])
        self.assertEqual(self.artifact(self.first), saved_first)
        self.assertEqual(self.artifact(self.second), unchanged_second)
        self.execute_approved_transaction.assert_called_once_with(self.first)
        self.choose_queue_row(1)
        self.assertTrue(self.window._active_review)
        self.assertEqual(self.window.transaction_id, self.second)

    def test_open_popup_is_not_reset_by_timer_refresh_or_new_rows(self):
        picker = self.window.queue_choices
        picker.showPopup()
        self.window.qt_app.processEvents()
        view = picker.view()
        view.setCurrentIndex(view.model().index(1, 0))
        resets = Mock()
        picker.model().modelReset.connect(resets)
        self.add_action(3)
        QTest.qWait(1100)
        self.assertTrue(view.isVisible())
        self.assertEqual(view.currentIndex().row(), 1)
        resets.assert_not_called()
        self.assertEqual(picker.count(), 2)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(view.currentIndex()).center())
        self.window.qt_app.processEvents()
        self.assertEqual(self.window.transaction_id, self.second)
        self.assertEqual(self.window.queue_choices.count(), 3)
        self.assertFalse(self.artifact(self.second)["approvals"])
        self.execute_approved_transaction.assert_not_called()

    def test_planned_rows_without_transactions_keep_distinct_selection(self):
        third, _ = self.add_planned_action(3)
        fourth, _ = self.add_planned_action(4)
        self.window.refresh_approval_state()
        self.choose_queue_row(2)
        self.assertEqual(self.window.queue_choices.currentData(), third)
        self.window.refresh_approval_state()
        self.assertEqual(self.window.queue_choices.currentData(), third)
        self.choose_queue_row(3)
        self.assertEqual(self.window.queue_choices.currentData(), fourth)
        self.assertIn("Planned4", self.window.queue_choices.currentText())
        self.assertFalse(self.window.execute_button.isEnabled())
        self.assertEqual(self.state.session_transactions(third), [])
        self.assertEqual(self.state.session_transactions(fourth), [])

    def test_selected_plan_follows_its_new_approval_after_readonly_refresh(self):
        selected, plan = self.add_planned_action(3)
        self.window.refresh_approval_state()
        self.choose_queue_row(2)
        tx = self.prepare_planned_action(selected, plan)
        self.window.refresh_approval_state()
        self.assertTrue(self.window._active_review)
        self.assertEqual(self.window.transaction_id, tx)
        self.assertEqual(self.window.queue_choices.currentData(), selected)
        self.assertFalse(self.window.execute_button.isEnabled())
        self.assertFalse(self.artifact(tx)["approvals"])
        self.execute_approved_transaction.assert_not_called()

    def test_readonly_popup_keeps_choice_until_new_approval_can_be_shown(self):
        selected, plan = self.add_planned_action(3)
        self.window.refresh_approval_state()
        self.choose_queue_row(2)
        picker = self.window.queue_choices
        page = self.window.root.centralWidget()
        picker.showPopup()
        self.window.qt_app.processEvents()
        tx = self.prepare_planned_action(selected, plan)
        self.window.refresh_approval_state()
        self.assertIs(self.window.queue_choices, picker)
        self.assertIs(self.window.root.centralWidget(), page)
        self.assertTrue(picker.view().isVisible())
        self.assertFalse(self.window._active_review)
        picker.hidePopup()
        self.window.refresh_approval_state()
        self.assertTrue(self.window._active_review)
        self.assertEqual(self.window.transaction_id, tx)
        self.assertFalse(self.artifact(tx)["approvals"])
        self.execute_approved_transaction.assert_not_called()

    def test_readonly_state_change_during_popup_is_rendered_after_close(self):
        selected, _ = self.add_planned_action(3)
        self.window.refresh_approval_state()
        self.choose_queue_row(2)
        picker = self.window.queue_choices
        picker.showPopup()
        self.window.qt_app.processEvents()
        other = self.state.create_conversation()["conversation_id"]
        self.state.transfer_session(selected, other, expected_owner=self.owner, expected_revision=0,
                                    expected_transaction_id=None)
        self.window.refresh_approval_state()
        self.assertIs(self.window.queue_choices, picker)
        self.assertTrue(picker.view().isVisible())
        self.assertIn("尚未提交审批", self.window.progress.text())
        picker.hidePopup()
        self.window.refresh_approval_state()
        self.assertIn("已转交", self.window.badge.text())
        self.assertEqual(self.window.queue_choices.currentData(), selected)
        self.assertEqual(self.window.queue_details.count(), 1)
        self.assertFalse(self.window.execute_button.isEnabled())
        self.execute_approved_transaction.assert_not_called()

    def test_uncertain_failure_stops_queue_and_keeps_transaction_locked(self):
        def fail(tx):
            self.state.claim_transaction(tx)
            self.state.update_transaction(tx, "failed", "unknown commit result")
            raise RuntimeError("unknown commit result")
        self.execute_approved_transaction.side_effect = fail
        self.save_current()
        self.assertTrue(self.window._queue_stopped)
        self.assertEqual(self.window.transaction_id, self.first)
        self.assertFalse(self.window.execute_button.isEnabled())
        self.assertFalse(self.artifact(self.second)["approvals"])
        self.window._select_queue_item(1)
        self.assertEqual(self.window.transaction_id, self.first)
        with self.assertRaises(ApprovalWindowBusyError):
            with approval_window_lock(self.state.get_transaction(self.first)["artifact_path"]):
                self.fail("Uncertain action lock was released")

    def test_corrupt_next_artifact_cannot_show_stale_review_or_approve(self):
        path = self.state.get_transaction(self.second)["artifact_path"]
        artifact = self.artifact(self.second)
        artifact["desired_design"]["js_code"] = "tampered"
        save_artifact(path, artifact)
        self.save_current()
        self.assertFalse(self.window._queue_stopped)
        self.assertFalse(self.window._active_review)
        self.assertIn(self.second, self.window._isolated_candidates)
        self.assertFalse(self.window.retry_candidates_button.isHidden())
        self.window.approve_current()
        self.window.execute()
        self.assertFalse(self.artifact(self.second)["approvals"])
        self.execute_approved_transaction.assert_called_once_with(self.first)

    def test_bad_unexecuted_candidate_is_isolated_and_healthy_later_action_continues(self):
        third = self.add_action(3)
        path = Path(self.state.get_transaction(self.second)["artifact_path"])
        original = path.read_text(encoding="utf-8")
        path.write_text("{broken json", encoding="utf-8")
        self.save_current()
        self.assertFalse(self.window._queue_stopped)
        self.assertTrue(self.window._active_review)
        self.assertEqual(self.window.transaction_id, third)
        self.assertIn(self.second, self.window._isolated_candidates)
        self.assertFalse(self.artifact(third)["approvals"])
        with approval_window_lock(path):
            pass  # Isolation does not retain the failed candidate's window lock.
        path.write_text(original, encoding="utf-8")
        self.window._retry_isolated_candidates()
        self.assertEqual(self.window.transaction_id, self.second)
        self.assertNotIn(self.second, self.window._isolated_candidates)
        self.assertFalse(self.artifact(self.second)["approvals"])
        self.execute_approved_transaction.assert_called_once_with(self.first)

    def test_execution_started_artifact_cannot_be_isolated_as_corrupt(self):
        path = self.state.get_transaction(self.second)["artifact_path"]
        artifact = self.artifact(self.second)
        artifact["execution_started"] = True
        artifact["desired_design"]["js_code"] = "corrupt"
        save_artifact(path, artifact)
        self.save_current()
        self.assertTrue(self.window._queue_stopped)
        self.assertNotIn(self.second, self.window._isolated_candidates)
        self.execute_approved_transaction.assert_called_once_with(self.first)

    def test_transferred_pending_action_is_visible_but_not_reviewed(self):
        other = self.state.create_conversation()["conversation_id"]
        session = self.artifact(self.second)["session_id"]
        self.state.transfer_session(session, other, expected_owner=self.owner, expected_revision=0, expected_transaction_id=self.second)
        self.save_current()
        self.assertFalse(self.window._active_review)
        self.assertIn("已转交", self.window.queue_choices.itemText(1))
        self.assertFalse(self.artifact(self.second)["approvals"])

    def test_busy_next_window_is_waited_for_then_resumed(self):
        with approval_window_lock(self.state.get_transaction(self.second)["artifact_path"]):
            self.save_current()
            self.assertFalse(self.window._active_review)
            self.assertTrue(self.window._waiting_for_lock)
        self.window.refresh_approval_state()
        self.assertEqual(self.window.transaction_id, self.second)
        self.assertTrue(self.window._active_review)
        self.assertFalse(self.artifact(self.second)["approvals"])

    def test_queue_window_is_unique_and_small_layout_keeps_buttons_reachable(self):
        with self.assertRaises(ApprovalWindowBusyError):
            self.app.ApprovalQueueWindow(self.queue, self.owner)
        self.window.root.resize(640, 420)
        self.window.qt_app.processEvents()
        for button in (self.window.approve_button, self.window.execute_button, self.window.close_button):
            point = button.mapTo(self.window.root, button.rect().center())
            self.assertIs(self.window.root.childAt(point), button)
        self.messagebox.askyesno.return_value = False
        QTest.mouseClick(self.window.approve_button, Qt.MouseButton.LeftButton)
        self.assertFalse(self.artifact(self.first)["approvals"])
        self.execute_approved_transaction.assert_not_called()


if __name__ == "__main__":
    unittest.main()
