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
        result = review_response(review, "plan-1")
        self.assertEqual(before, review)
        self.assertEqual(review["review_sha256"], result["report_sha256"])
        self.assertEqual("unverified", result["review_status"])
        self.assertIn("human_review", result["evidence_gaps"][0]["can_continue"])
        self.assertEqual("read_plan_review", result["evidence_gaps"][0]["next_read"]["tool"])

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
