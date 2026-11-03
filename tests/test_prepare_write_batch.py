"""Multiple generated action plans enter human approval together, without business IO."""
from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from tests.helpers import baseline
from tests.payload_fixtures import PayloadHarness
from gxp_editor.approval import load_artifact, save_artifact
from gxp_editor.compiler import CompilerError
from gxp_editor.execution_lock import ApprovalWindowBusyError, approval_queue_lock, execution_lock
from gxp_editor.service import EditorServiceError


class PrepareWriteBatchTests(unittest.TestCase):
    def setUp(self):
        self.h = self.enterContext(PayloadHarness())
        self.service, self.store = self.h.service, self.h.service.state
        self.enterContext(patch("gxp_editor.config.runtime_root", return_value=self.h.root))
        self.queue = self.service.create_edit_queue()["queue_id"]
        self.drafts = {}
        for index in range(1, 4):
            draft = baseline(**{**deepcopy(self.h.draft), "id": f"design-{index}", "ref_id": f"ref-{index}",
                                "code": f"Action{index}", "metadata": {"action_code": f"Action{index}"}})
            self.drafts[draft["id"]] = draft
        self.h.repository.load_draft_by_identifier = lambda identifier: deepcopy(next(
            draft for draft in self.drafts.values() if draft["code"] == identifier))
        self.h.repository.load_draft_by_id = lambda design_id, **kwargs: deepcopy(self.drafts[design_id])
        self.sessions, self.plans = [], []
        for index in range(1, 4):
            session = self.service.begin_edit_session(f"Action{index}", queue_id=self.queue)["session_id"]
            plan = self.service.plan_canvas_patch(session, [{"operation": "set_parameter",
                "action_group_key": "main", "target_node_key": "target", "expected_element_key": "Noop",
                "path": "/inputParams/settings/flag", "expected_old_value": False, "value": True}], self.h.spec())
            self.assertTrue(plan["ready_for_approval"])
            self.sessions.append(session)
            self.plans.append(plan["plan_id"])

    def transaction_count(self):
        with self.store._connection() as db:
            return db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    def partial_failure(self):
        original = self.service.prepare_write

        def prepare(plan_id, **kwargs):
            if plan_id == self.plans[1]:
                raise CompilerError("Synthetic second action validation failed")
            return original(plan_id, **kwargs)

        with patch.object(self.service, "prepare_write", side_effect=prepare), patch.object(
                self.service, "_try_launch_approval") as launch:
            result = self.service.prepare_write_batch(self.queue, self.plans, response_detail="summary")
        launch.assert_not_called()
        return result

    def test_all_actions_are_frozen_unsigned_before_the_single_window_launch(self):
        def launch(transaction_id):
            self.assertEqual(self.transaction_count(), 3)
            for session_id in self.sessions:
                session = self.store.get_session(session_id)
                self.assertEqual(session["state"], "awaiting_approval")
                tx = self.store.get_transaction(session["transaction_id"])
                artifact = load_artifact(tx["artifact_path"])
                self.assertEqual(artifact["approvals"], {})
                self.assertFalse(artifact.get("executed"))
            # Launch occurs after preparation releases the queue lifetime lock.
            with approval_queue_lock(self.queue):
                pass
            return {"approval_window_opened": True, "approval_window_starting": False}

        with patch.object(self.service, "_try_launch_approval", side_effect=launch) as window:
            result = self.service.prepare_write_batch(self.queue, self.plans, response_detail="summary")
        window.assert_called_once_with(result["transaction_ids"][0])
        self.assertTrue(result["batch_prepared"])
        self.assertEqual(result["prepared_count"], 3)
        self.assertEqual(result["statement_count"], 3)
        self.assertEqual(result["remaining_plan_ids"], [])
        self.assertEqual(result["workflow_phase"], "awaiting_human")
        self.assertFalse(result["approval_granted"])
        self.assertFalse(result["write_executed"])

    def test_partial_failure_retains_pending_ids_and_retry_does_not_duplicate_them(self):
        failed = self.partial_failure()
        self.assertFalse(failed["batch_prepared"])
        self.assertEqual(failed["prepared_count"], 1)
        self.assertEqual(failed["remaining_plan_ids"], self.plans[1:])
        self.assertEqual(failed["failure"]["plan_id"], self.plans[1])
        self.assertTrue(failed["approval_deferred"])
        self.assertEqual(failed["workflow_phase"], "batch_preparation_blocked")
        self.assertEqual(self.transaction_count(), 1)
        result = self.service.prepare_write_batch(self.queue, self.plans)
        self.assertTrue(result["batch_prepared"])
        self.assertEqual(result["transaction_ids"][0], failed["transaction_ids"][0])
        self.assertTrue(result["items"][0]["transaction_reused"])
        self.assertEqual(self.transaction_count(), 3)

    def test_held_queue_window_blocks_preparation_before_any_transaction(self):
        with approval_queue_lock(self.queue):
            with self.assertRaises(ApprovalWindowBusyError):
                self.service.prepare_write_batch(self.queue, self.plans)
        self.assertEqual(self.transaction_count(), 0)

    def test_launch_reservation_blocks_preparation_before_any_transaction(self):
        with self.store._connection() as db:
            db.execute("UPDATE queue_runtime SET launch_state='reserved' WHERE queue_id=?", (self.queue,))
        with self.assertRaisesRegex(EditorServiceError, "existing queue approval window"):
            self.service.prepare_write_batch(self.queue, self.plans)
        self.assertEqual(self.transaction_count(), 0)

    def test_invalid_or_duplicate_inputs_never_create_partial_transactions(self):
        for values in ([], self.plans + [self.plans[0]], [self.plans[0], ""], [None]):
            with self.subTest(values=values), self.assertRaises(EditorServiceError):
                self.service.prepare_write_batch(self.queue, values)
        self.assertEqual(self.transaction_count(), 0)

    def test_stale_later_plan_is_rejected_before_the_first_plan_is_prepared(self):
        self.store.create_plan(self.sessions[1], "unused.json", "old", "new", "compiler",
                               conversation_id=self.h.owner)
        with self.assertRaisesRegex(EditorServiceError, "current owned plan"):
            self.service.prepare_write_batch(self.queue, self.plans)
        self.assertEqual(self.transaction_count(), 0)

    def test_foreign_plan_is_rejected_before_the_first_plan_is_prepared(self):
        other = self.store.create_conversation()["conversation_id"]
        queue = self.store.create_edit_queue(other)
        session = self.store.create_session(baseline(id="foreign", ref_id="foreign-ref"), "backup", "compiler",
                                             conversation_id=other, queue_id=queue)
        plan = self.store.create_plan(session["id"], "unused.json", "old", "new", "compiler", conversation_id=other)
        with self.assertRaises(EditorServiceError):
            self.service.prepare_write_batch(self.queue, [self.plans[0], plan])
        self.assertEqual(self.transaction_count(), 0)

    def test_pending_execution_marker_prevents_reuse_and_window_launch(self):
        failed = self.partial_failure()
        tx = self.store.get_transaction(failed["transaction_ids"][0])
        artifact = load_artifact(tx["artifact_path"])
        artifact["execution_started"] = True
        save_artifact(tx["artifact_path"], artifact)
        with patch.object(self.service, "_try_launch_approval") as launch:
            result = self.service.prepare_write_batch(self.queue, self.plans)
        launch.assert_not_called()
        self.assertFalse(result["batch_prepared"])
        self.assertEqual(result["prepared_count"], 0)
        self.assertEqual(self.transaction_count(), 1)

    def test_pending_execution_lock_prevents_reuse_and_window_launch(self):
        failed = self.partial_failure()
        tx = self.store.get_transaction(failed["transaction_ids"][0])
        with execution_lock(tx["artifact_path"]), patch.object(self.service, "_try_launch_approval") as launch:
            result = self.service.prepare_write_batch(self.queue, self.plans)
        launch.assert_not_called()
        self.assertFalse(result["batch_prepared"])
        self.assertEqual(self.transaction_count(), 1)

    def test_pending_draft_change_prevents_reuse_before_other_actions_prepare(self):
        self.partial_failure()
        self.drafts["design-1"]["js_code"] = "external draft modification"
        with patch.object(self.service, "_try_launch_approval") as launch:
            result = self.service.prepare_write_batch(self.queue, self.plans)
        launch.assert_not_called()
        self.assertEqual(result["failure"]["reason_code"], "draft_baseline_changed")
        self.assertEqual(self.transaction_count(), 1)

    def test_failed_window_launch_keeps_all_transactions_available(self):
        with patch.object(self.service, "_try_launch_approval", return_value={"approval_window_opened": False,
                "approval_window_starting": False, "approval_window_error": "Synthetic launcher failure"}):
            result = self.service.prepare_write_batch(self.queue, self.plans)
        self.assertTrue(result["batch_prepared"])
        self.assertEqual(result["workflow_phase"], "window_problem")
        self.assertEqual(self.transaction_count(), 3)
        self.assertFalse(result["write_executed"])


if __name__ == "__main__":
    unittest.main()
