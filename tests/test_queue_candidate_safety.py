"""Offline regressions for candidate quarantine and pre-claim failure routing."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_qt_approval_queue as queue_fixture
from gxp_editor.approval import save_artifact, transaction_binding_hash
from gxp_editor.queue_protocol import ISOLATED_ENV
from gxp_editor.service import EditorService
from gxp_editor.writer import WriteExecutionError


class QueueCandidateSafetyTests(unittest.TestCase):
    setUp = queue_fixture.QtApprovalQueueTests.setUp
    add_action = queue_fixture.QtApprovalQueueTests.add_action
    open_window = queue_fixture.QtApprovalQueueTests.open_window
    dispose = staticmethod(queue_fixture.QtApprovalQueueTests.dispose)
    artifact = queue_fixture.QtApprovalQueueTests.artifact
    save_offline = queue_fixture.QtApprovalQueueTests.save_offline
    save_current = queue_fixture.QtApprovalQueueTests.save_current

    def launch_queue(self, preflight=None):
        self.window.close()
        service = object.__new__(EditorService)
        service._conversation_id = self.owner
        service.state = self.state
        with patch.object(service, "_approval_preflight", side_effect=preflight, return_value={}), patch(
                "gxp_editor.ui_launch.inspect_window_startup", return_value={
                    "startup_status": "not_started", "process_state": "exited"}), patch(
                "gxp_editor.ui_launch.launch_window", return_value={"window_opened": True}) as launch:
            result = service._launch_approval(self.first)
        return result, launch.call_args.kwargs["extra_env"]

    def test_corrupt_pending_candidate_does_not_block_launch_or_healthy_actions(self):
        third = self.add_action(3)
        Path(self.state.get_transaction(self.second)["artifact_path"]).write_text("{broken", encoding="utf-8")
        result, environment = self.launch_queue()
        self.assertTrue(result["window_opened"])
        self.assertEqual(json.loads(environment[ISOLATED_ENV]), {self.second: "candidate_preflight_failed"})
        with patch.dict(os.environ, environment):
            self.window = self.open_window()
            self.assertIn(self.second, self.window._isolated_candidates)
            self.save_current()
            self.assertEqual(self.window.transaction_id, third)
            self.assertFalse(self.window._queue_stopped)
            self.assertFalse(self.artifact(third)["approvals"])
            self.window.close()
        self.assertEqual(self.state.get_transaction(self.second)["state"], "awaiting_approval")

    def test_stale_preflight_is_isolated_without_consuming_or_cancelling_approval(self):
        original = Path(self.state.get_transaction(self.second)["artifact_path"]).read_bytes()
        def preflight(transaction, session, artifact):
            if transaction["id"] == self.second:
                raise RuntimeError("synthetic stale baseline")
            return {}
        _, environment = self.launch_queue(preflight)
        self.assertIn(self.second, json.loads(environment[ISOLATED_ENV]))
        self.assertEqual(Path(self.state.get_transaction(self.second)["artifact_path"]).read_bytes(), original)
        self.assertEqual(self.state.get_transaction(self.second)["state"], "awaiting_approval")

    def test_conflicting_identity_or_execution_marker_never_becomes_isolation(self):
        original = self.artifact(self.second)
        for field, value in (("transaction_id", "other-transaction"), ("execution_started", True)):
            with self.subTest(field=field):
                changed = {**original, field: value}
                changed["transaction_hash"] = transaction_binding_hash(changed)
                save_artifact(self.state.get_transaction(self.second)["artifact_path"], changed)
                with self.assertRaisesRegex(Exception, "身份不一致|执行标记"):
                    self.launch_queue()
                self.assertEqual(self.state.get_transaction(self.second)["state"], "awaiting_approval")

    def test_state_claim_during_preflight_still_blocks_queue_launch(self):
        def preflight(transaction, session, artifact):
            if transaction["id"] == self.second:
                self.state.claim_transaction(self.second)
            return {}
        with self.assertRaisesRegex(Exception, "执行状态未核实"):
            self.launch_queue(preflight)
        self.assertEqual(self.state.get_transaction(self.second)["state"], "executing")

    def test_preclaim_failure_guides_replanning_and_keeps_other_reviews_available(self):
        error = WriteExecutionError("synthetic compiler drift")
        error.code = "compiler_binding_changed"
        error.details = {"execution_claimed": False, "replan_required": True}
        self.execute_approved_transaction.side_effect = error
        self.save_current()
        self.assertFalse(self.window._queue_stopped)
        self.assertEqual(self.window.transaction_id, self.second)
        self.assertIn(self.first, self.window._isolated_candidates)
        self.assertIn("取消失效审批并重新规划", self.window.queue_result.text())
        self.assertNotIn("恢复该会话", self.window.queue_result.text())
        self.assertEqual(self.state.get_transaction(self.first)["state"], "awaiting_approval")
        self.assertTrue(self.artifact(self.first)["approvals"])
        self.assertFalse(self.artifact(self.second)["approvals"])

    def test_claim_markers_override_preclaim_error_hint(self):
        def fail(transaction_id):
            artifact = self.artifact(transaction_id)
            artifact["execution_started"] = True
            save_artifact(self.state.get_transaction(transaction_id)["artifact_path"], artifact)
            error = WriteExecutionError("synthetic ambiguous execution")
            error.details = {"execution_claimed": False}
            raise error
        self.execute_approved_transaction.side_effect = fail
        self.save_current()
        self.assertTrue(self.window._queue_stopped)
        self.assertNotIn(self.first, self.window._isolated_candidates)
        self.assertFalse(self.artifact(self.second)["approvals"])

    def test_worker_preserves_structured_preclaim_reason(self):
        error = WriteExecutionError("synthetic compiler drift")
        error.code = "compiler_binding_changed"
        error.details = {"execution_claimed": False, "next_action": "inspect_session_cancellation"}
        self.execute_approved_transaction.side_effect = error
        worker = self.app.ExecutionThread(self.first)
        failures = []
        worker.failed.connect(failures.append)
        worker.run()
        self.assertEqual(failures, [{"message": str(error), "reason_code": error.code, "error_details": error.details}])

    def test_single_window_preclaim_failure_does_not_request_inapplicable_recovery(self):
        self.window.close()
        self.window = self.app.ApprovalWindow(self.first, self.owner, 0)
        self.addCleanup(self.dispose, self.window)
        self.window.root.show()
        self.execute_approved_transaction.side_effect = WriteExecutionError("synthetic compiler drift")
        self.save_current()
        self.assertTrue(self.window._review_blocked)
        self.assertIn("未进入执行", self.window.progress.text())
        self.assertIn("取消失效审批并重新规划", self.window.progress.text())
        self.assertNotIn("恢复会话", self.window.progress.text())
        self.assertEqual(self.state.get_transaction(self.first)["state"], "awaiting_approval")
        self.assertTrue(self.artifact(self.first)["approvals"])


if __name__ == "__main__":
    unittest.main()
