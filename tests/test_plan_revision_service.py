"""Saved-plan revisions keep scope, ownership and current-plan CAS checks."""
from __future__ import annotations

import copy
import unittest

from tests import test_business_review_service as review_fixtures
from gxp_editor.approval import load_artifact
from gxp_editor.hashing import sha256_json
from gxp_editor.service import EditorServiceError


class PlanRevisionServiceTests(unittest.TestCase):
    def setUp(self):
        self.flow = review_fixtures.BusinessReviewServiceTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.first = self.flow.plan(review_fixtures.review_spec())

    def test_read_operation_hash_then_replace_whole_operation_without_resending_others(self):
        service = self.flow.service
        page = service.read_plan_review(self.first["plan_id"], section="operations", detail="summary")
        self.assertEqual(page["items"][0]["operation_sha256"], sha256_json(self.flow.operations[0]))
        changed = copy.deepcopy(self.flow.operations[0])
        changed["input_params"]["value"] = "newer"
        result = service.revise_canvas_plan(self.first["plan_id"], replacements=[{
            "index": 0, "expected_operation_sha256": page["items"][0]["operation_sha256"], "operation": changed}])
        artifact = load_artifact(service.state.get_plan(result["plan_id"])["artifact_path"])
        self.assertEqual(artifact["operations"], [changed])
        self.assertEqual(result["revised_from_plan_id"], self.first["plan_id"])
        self.assertFalse(result["candidate_reused"])
        self.assertEqual(self.flow.compiler.compile_patch.call_args.args[1], [changed])

    def test_review_update_preserves_all_scenarios_and_requires_new_evidence_for_changed_rules(self):
        service = self.flow.service
        original = review_fixtures.review_spec()
        update = {"revision": "request-2", "requirements": [{**original["requirements"][0],
                  "text": "改后的规则说明", "source_text": "用户明确补充：编辑时必须保留原值。"}]}
        result = service.revise_canvas_plan(self.first["plan_id"], review_spec_update=update)
        artifact = load_artifact(service.state.get_plan(result["plan_id"])["artifact_path"])
        spec = artifact["business_review"]["spec"]
        self.assertEqual(spec["scenarios"], original["scenarios"])
        self.assertEqual(spec["requirements"][0]["source_text"], update["requirements"][0]["source_text"])
        bad = {"revision": "request-3", "requirements": [{**spec["requirements"][0], "targets": [{"group_key": "main"}]}]}
        with self.assertRaisesRegex(EditorServiceError, "新的用户依据"):
            service.revise_canvas_plan(result["plan_id"], review_spec_update=bad)

    def test_stale_reference_and_wrong_hash_never_compile_or_create_approval(self):
        service = self.flow.service
        calls = self.flow.compiler.compile_patch.call_count
        with self.assertRaises(ValueError):
            service.revise_canvas_plan(self.first["plan_id"], replacements=[{
                "index": 0, "expected_operation_sha256": "0" * 64, "operation": self.flow.operations[0]}])
        self.assertEqual(self.flow.compiler.compile_patch.call_count, calls)
        second = service.revise_canvas_plan(self.first["plan_id"])
        with self.assertRaisesRegex(EditorServiceError, "最新"):
            service.revise_canvas_plan(self.first["plan_id"])
        self.flow.launch.assert_not_called()
        self.assertIsNone(service.state.get_session(self.flow.session_id).get("transaction_id"))
        self.assertNotEqual(second["plan_id"], self.first["plan_id"])

    def test_full_and_partial_review_spec_cannot_be_combined(self):
        with self.assertRaisesRegex(EditorServiceError, "只能提供一个"):
            self.flow.service.revise_canvas_plan(self.first["plan_id"], review_spec=review_fixtures.review_spec(),
                                                review_spec_update={"revision": "request-2"})


if __name__ == "__main__":
    unittest.main()
