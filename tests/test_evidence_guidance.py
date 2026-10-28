import copy
import unittest

from gxp_editor.review_response import review_response
from gxp_editor.telemetry import traced_tool
from tests.test_approval import transaction


class EvidenceGuidanceTests(unittest.TestCase):
    def test_unverified_guidance_preserves_signed_artifact_and_human_route(self):
        review = transaction()["business_review"]
        review["status"] = "unverified"
        before = copy.deepcopy(review)
        result = review_response(review, "plan-1", conversation_id="owner")
        self.assertEqual(before, review)
        self.assertEqual(review["review_sha256"], result["report_sha256"])
        self.assertEqual("unverified", result["review_status"])
        self.assertIn("human_review", result["evidence_gaps"][0]["can_continue"])
        self.assertEqual("read_plan_review", result["evidence_gaps"][0]["next_read"]["tool"])
        self.assertEqual("owner", result["evidence_gaps"][0]["next_read"]["arguments"]["conversation_id"])

    def test_returned_approval_failure_has_owned_status_route_without_relaunching(self):
        original = {"transaction_id": "tx", "reason_code": "approval_not_pending",
                    "approval_window_opened": False, "approval_window_starting": False,
                    "approval_window_error": "Only a pending approval can reopen", "executed": False,
                    "execution_outcome": "unknown"}
        @traced_tool
        def reopen_approval(transaction_id, conversation_id=""):
            return original
        before = copy.deepcopy(original)
        result = reopen_approval("tx", "owner")
        self.assertTrue(result["blocked"])
        self.assertEqual("approval_not_pending", result["error_code"])
        self.assertEqual("reopen_approval", result["failure_stage"])
        self.assertEqual(result["next_read"], {"tool": "get_write_status",
            "arguments": {"transaction_id": "tx", "conversation_id": "owner"}})
        self.assertEqual(result["next_read"], result["evidence_gaps"][0]["next_read"])
        self.assertEqual(["reopen_approval"], result["fallback"]["attempted"])
        self.assertEqual("unknown", result["execution_outcome"])
        self.assertNotIn("write_executed", result)
        self.assertEqual(original, before)

    def test_failed_read_does_not_suggest_repeating_the_same_failed_path(self):
        @traced_tool
        def get_write_status(transaction_id, conversation_id=""):
            return {"error": "fixture unavailable", "reason_code": "status_unavailable"}
        result = get_write_status("tx", "owner")
        self.assertEqual("status_unavailable", result["error_code"])
        self.assertEqual(result["next_read"], {"tool": "editor_status", "arguments": {"conversation_id": "owner"}})

    def test_successful_startup_reason_is_not_misclassified_as_failure(self):
        @traced_tool
        def prepare_write():
            return {"reason_code": "already_open", "approval_window_opened": True}
        result = prepare_write()
        self.assertNotIn("blocked", result)
        self.assertNotIn("fallback", result)

    def test_failed_guidance_does_not_offer_human_waiver(self):
        review = transaction()["business_review"]
        review["status"] = "failed"
        result = review_response(review, "plan-1")
        self.assertEqual("failed_review_blocks_approval", result["fallback"]["stop_reason"])
        self.assertNotIn("human_review", result["evidence_gaps"][0]["can_continue"])

    def test_error_response_remains_blocked_and_does_not_mutate_artifact(self):
        original = {"blocked": True, "error_code": "COMPILER_DRIFT", "write_executed": False}
        result = traced_tool(lambda: original)()
        self.assertTrue(result["blocked"])
        self.assertFalse(result["write_executed"])
        self.assertNotIn("evidence_gaps", original)
