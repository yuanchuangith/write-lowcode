from __future__ import annotations

import unittest

from gxp_editor.workflow_guidance import write_status_guidance


class WorkflowGuidanceTests(unittest.TestCase):
    def guide(self, state="awaiting_approval", session_state="awaiting_approval", startup=None, **kwargs):
        return write_status_guidance({"id": "tx", "state": state},
            {"state": session_state, "transaction_id": "tx"}, startup or {}, **kwargs)

    def test_startup_then_human_wait_then_execution_then_completion_trace(self):
        responses = [self.guide(startup={"startup_status": "starting", "window_starting": True}),
                     self.guide(startup={"startup_status": "ready", "window_opened": True}),
                     self.guide(state="executing", startup={"startup_status": "ready", "window_executing": True}),
                     self.guide(state="saved", session_state="finished")]
        self.assertEqual([r["next_action"] for r in responses],
            ["get_write_status", "wait_for_user", "get_write_status", "report_completion"])
        self.assertGreater(responses[1]["poll_after_ms"], responses[0]["poll_after_ms"])
        self.assertIsNone(responses[-1]["poll_after_ms"])
        self.assertEqual(responses[0]["startup_check_budget_ms"], 30000)
        self.assertTrue(all(r["progress_update_after_ms"] == 30000 for r in responses))

    def test_startup_timeout_and_dismissal_do_not_reopen_a_window(self):
        for state in ("startup_timeout", "heartbeat_timeout", "failed", "closed"):
            with self.subTest(state=state):
                result = self.guide(startup={"startup_status": state})
                self.assertIsNone(result["poll_after_ms"])
                self.assertNotIn(result["next_action"], {"prepare_write", "reopen_approval"})
        self.assertEqual(self.guide(startup={"startup_status": "closed"})["next_action"], "wait_for_continuation")

    def test_uncertain_execution_routes_to_recovery_without_guessing_completion(self):
        for state in ("failed", "saved_state_pending"):
            self.assertEqual(self.guide(state=state)["next_action"], "recover_edit_session")
        self.assertEqual(self.guide(state="executing", startup={"startup_status": "failed"})["next_action"], "recover_edit_session")

    def test_unfinished_saves_route_through_verification_and_release(self):
        self.assertEqual(self.guide("saved", "saved")["next_action"], "verify_saved_draft")
        self.assertEqual(self.guide("saved", "verified")["next_action"], "finish_edit_session")
        self.assertEqual(self.guide("saved", "closed_unverified")["next_action"], "report_outcome")

    def test_invalid_artifact_and_previous_save_never_trigger_new_approval(self):
        self.assertEqual(self.guide(artifact_valid=False)["next_action"], "inspect_session_cancellation")
        result = write_status_guidance({"id": "old", "state": "saved"},
            {"state": "restore_pending", "transaction_id": "restore"}, {})
        self.assertEqual(result["workflow_phase"], "historical")
        self.assertIsNone(result["poll_after_ms"])
