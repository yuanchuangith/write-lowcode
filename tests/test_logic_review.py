from __future__ import annotations

import copy
import json
import unittest

from tests.helpers import baseline, editor_config, transaction
from gxp_editor.approval import approve_statement, transaction_binding_hash, verify_approvals
from gxp_editor.hashing import sha256_json, sha256_text
from gxp_editor.logic_review import (
    LogicReviewError, build_business_review, normalize_review_spec, plan_binding_hash,
    require_write_business_review, validate_business_review,
)
from gxp_editor.transaction import TransactionPlanError, build_transaction_artifact, validate_transaction_artifact
from unittest.mock import patch


def specification():
    return {"schema_version": 1, "revision": "request-1", "requirements": [
        {"id": "retain", "text": "编辑时保留原值", "source_text": "用户：编辑时保留原值",
         "targets": [{"group_key": "main", "node_key": "assign"}]}],
        "scenarios": {"schema_version": 1, "cases": [
            {"id": "edit", "requirement_ids": ["retain"],
             "environment": {"fields": {"target": {"value": "old"}}},
             "steps": [{"kind": "call_group", "group_key": "main", "args": []}],
             "expect": {"fields": {"target": {"value": "old"}}}}]}}


class LogicReviewTests(unittest.TestCase):
    def setUp(self):
        self.template = transaction()
        self.base = baseline()
        self.desired = {**self.template["desired_design"], "relation_data": {}}
        self.binding = self.template["compiler_evidence"]["compiler_binding"]

    def report(self, spec, outcome="passed", baseline_outcome="failed"):
        return {"schema_version": 1, "runner_version": "fixture-1", "spec_sha256": sha256_json(spec["scenarios"]),
                "baseline_source_sha256": sha256_text(self.base["js_code"]),
                "candidate_source_sha256": sha256_text(self.desired["js_code"]),
                "cases": [{"id": case["id"], "requirement_ids": case.get("requirement_ids", []),
                           "expected": copy.deepcopy(case["expect"]), "baseline": {"status": baseline_outcome},
                           "candidate": {"status": outcome}}
                          for case in spec["scenarios"]["cases"]]}

    def build(self, spec=None, outcome="passed", **kwargs):
        if spec is None:
            spec = {"schema_version": 1, "revision": "manual-fixture", "scope_policy": "manual_review",
                    "requirements": [], "scenarios": {"schema_version": 1, "cases": []}}
        return build_business_review(review_spec=spec, baseline_design=self.base, candidate_design=self.desired,
                                     compiler_binding=self.binding,
                                     scenario_report=None if spec is None else self.report(spec, outcome), **kwargs)

    @staticmethod
    def rehash(review):
        review["review_sha256"] = sha256_json({k: v for k, v in review.items() if k != "review_sha256"})

    def test_missing_expectations_are_explicitly_unverified_and_allowed_for_human_review(self):
        review = self.build()
        self.assertEqual(review["status"], "unverified")
        self.assertFalse(review["candidate_gate"]["blocked"])
        self.assertFalse(review["runtime_verified"])
        require_write_business_review(self.template)

    def test_failed_baseline_does_not_block_passing_candidate(self):
        canvas = {"actionData": [{"key": "main", "actionType": "js", "data": [{"key": "assign"}]}], "globalVariables": []}
        self.base = baseline(data=json.dumps(canvas))
        self.desired["data"] = json.dumps(canvas)
        spec = specification()
        # This case asserts the group's observable result. The runner does not
        # trace individual nodes, so it cannot certify a node-specific target.
        spec["requirements"][0]["targets"] = [{"group_key": "main"}]
        report = self.report(spec)
        report["cases"][0]["candidate"]["trace"] = [{"api": "group.main"}]
        review = build_business_review(review_spec=spec, baseline_design=self.base, candidate_design=self.desired,
                                       compiler_binding=self.binding, scenario_report=report)
        self.assertEqual(review["status"], "passed")
        self.assertEqual(review["requirements"][0]["status"], "passed")
        self.assertFalse(review["candidate_gate"]["blocked"])

    def test_failed_candidate_is_readable_but_rejected_by_final_gate(self):
        review = self.build(specification(), "failed")
        validate_business_review(review)
        artifact = {**self.template, "business_review": review}
        with self.assertRaisesRegex(LogicReviewError, "失败"):
            require_write_business_review(artifact)

    def test_unlinked_requirement_never_becomes_green(self):
        spec = specification()
        spec["scope_policy"] = "manual_review"
        spec["requirements"].append({"id": "extra", "text": "另一个要求", "source_text": "用户：另一个要求", "targets": []})
        review = self.build(spec)
        self.assertEqual(review["requirements"][1]["status"], "unverified")
        self.assertEqual(review["status"], "unverified")

    def test_extra_real_changes_are_unverified_without_overruling_human_review(self):
        self.desired["data"] = json.dumps({"actionData": [], "globalVariables": [{"name": "extra", "value": 1}]})
        spec = specification()
        spec["scope_policy"] = "manual_review"
        review = self.build(spec)
        self.assertTrue(review["changes"])
        self.assertEqual(review["changes"][0]["status"], "unverified")
        self.assertEqual(review["status"], "unverified")
        self.assertFalse(review["candidate_gate"]["blocked"])

    def test_group_and_node_order_unknown_fields_and_null_vs_missing_are_not_lost(self):
        old = {"actionData": [
            {"key": "a", "data": [{"key": "first", "paramsValue": {}}, {"key": "second", "paramsValue": {}}]},
            {"key": "b", "data": []}], "globalVariables": []}
        new = copy.deepcopy(old)
        new["actionData"].reverse()
        new["actionData"][1]["data"].reverse()
        new["actionData"][1]["data"][0]["unknown"] = None
        self.base = baseline(data=json.dumps(old))
        self.desired["data"] = json.dumps(new)
        review = self.build()
        paths = [change["field_path"] for change in review["changes"]]
        self.assertIn("actionData/$order", paths)
        self.assertIn("$order", paths)
        change = next(item for item in review["changes"] if item["field_path"] == "unknown")
        self.assertFalse(change["before_present"])
        self.assertTrue(change["after_present"])
        self.assertIsNone(change["after"])

    def test_reorder_positions_exclude_inserted_and_deleted_nodes(self):
        from gxp_editor.logic_review import _canvas_changes
        def canvas(keys):
            return {"actionData": [{"key": "main", "data": [{"key": key} for key in keys]}], "globalVariables": []}
        spec = {"requirements": [{"id": "order", "targets": [{"group_key": "main"}]}], "scope_policy": "strict"}
        changes = _canvas_changes(canvas(["deleted", "a", "b", "c"]), canvas(["c", "inserted", "b", "a"]), spec)
        order = [(item["node_key"], item["before"], item["after"]) for item in changes if item["field_path"] == "$order"]
        self.assertEqual(order, [("c", 2, 0), ("a", 0, 2)])
        self.assertEqual({item["node_key"] for item in changes if item["field_path"] != "$order"}, {"inserted", "deleted"})

    def test_mismatched_or_tampered_scenario_sources_and_expectations_are_rejected(self):
        for mutate in (
            lambda r: r["scenarios"].__setitem__("candidate_source_sha256", "f" * 64),
            lambda r: r["scenarios"]["cases"][0].__setitem__("expected", {}),
            lambda r: r["scenarios"]["cases"].clear(),
            lambda r: r["candidate_gate"].__setitem__("blocked", True),
        ):
            with self.subTest(mutate=mutate):
                review = self.build(specification())
                mutate(review)
                self.rehash(review)
                with self.assertRaises(LogicReviewError):
                    validate_business_review(review)

    def test_actual_canvas_diff_is_recomputed_instead_of_trusting_report(self):
        self.desired["data"] = json.dumps({"actionData": [], "globalVariables": ["changed"]})
        review = self.build()
        review["changes"] = []
        self.rehash(review)
        with self.assertRaisesRegex(LogicReviewError, "真实完整画布差异"):
            validate_business_review(review, baseline_design=self.base, candidate_design=self.desired)

    def test_invalid_spec_never_degrades_to_unverified(self):
        for mutate in (
            lambda s: s.__setitem__("schema_version", True),
            lambda s: s["requirements"].append(copy.deepcopy(s["requirements"][0])),
            lambda s: s["requirements"][0].__setitem__("source_text", ""),
            lambda s: s["scenarios"]["cases"][0].__setitem__("requirement_ids", ["missing"]),
        ):
            with self.subTest(mutate=mutate):
                spec = specification()
                mutate(spec)
                with self.assertRaises(LogicReviewError):
                    normalize_review_spec(spec)

    def test_plan_binding_includes_rules_report_and_candidate(self):
        artifact = {"base_content_hash": self.base["content_hash"], "compiler_binding": self.binding,
                    "hashes": self.template["compiler_evidence"]["hashes"], "business_review": self.build()}
        before = plan_binding_hash(artifact)
        artifact["business_review"]["spec"]["revision"] = "changed"
        self.assertNotEqual(before, plan_binding_hash(artifact))

    def test_old_write_remains_readable_but_cannot_be_executed(self):
        artifact = self.template
        artifact.pop("business_review")
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        validate_transaction_artifact(artifact)
        with self.assertRaisesRegex(LogicReviewError, "旧审批"):
            require_write_business_review(artifact)
        restore = transaction(kind="restore", desired_relations=[])
        validate_transaction_artifact(restore)
        require_write_business_review(restore)

    def test_review_change_revokes_human_signature_even_with_recomputed_transaction_hash(self):
        artifact = self.template
        config = editor_config()
        with patch("gxp_editor.approval.approval_secret", return_value="fixture-secret"):
            for statement in artifact["statements"]:
                approve_statement(config, artifact, statement["id"])
            artifact["business_review"]["spec"]["revision"] = "changed"
            self.rehash(artifact["business_review"])
            artifact["transaction_hash"] = transaction_binding_hash(artifact)
            with self.assertRaisesRegex(Exception, "Transaction metadata changed"):
                verify_approvals(config, artifact)

    def test_new_transaction_builder_rejects_known_failed_review(self):
        review = self.build(specification(), "failed")
        with self.assertRaisesRegex(TransactionPlanError, "失败"):
            build_transaction_artifact(
                transaction_id="new", session_id="session-1", plan_id="plan-1", kind="write",
                baseline=self.base, desired_design=self.desired, current_relations=[], desired_relations={},
                database_safety=self.template["database_safety"], runtime_validation=self.template["runtime_validation"],
                compiler_evidence=self.template["compiler_evidence"], business_review=review,
            )

    def test_runner_version_changes_require_replanning_but_old_report_stays_readable(self):
        review = self.build(specification())
        review["scenarios"]["runner_version"] = "old-version"
        self.rehash(review)
        validate_business_review(review)
        with self.assertRaisesRegex(LogicReviewError, "验证器版本已变化"):
            require_write_business_review({**self.template, "business_review": review})

    def test_pass_without_explicit_assertions_is_rejected(self):
        spec = specification()
        spec["scenarios"]["cases"][0]["expect"] = {"fields": {}, "call_counts": {}}
        with self.assertRaisesRegex(LogicReviewError, "显式预期断言"):
            self.build(spec)

    def test_unexecuted_scenarios_can_only_enter_human_review_as_unverified(self):
        review = build_business_review(review_spec=specification(), baseline_design=self.base,
                                      candidate_design=self.desired, compiler_binding=self.binding)
        require_write_business_review({**self.template, "business_review": review})
        self.assertEqual(review["status"], "unverified")

    def test_boolean_to_number_change_is_not_hidden_by_python_equality(self):
        self.base = baseline(data=json.dumps({"actionData": [], "globalVariables": [{"value": True}]}))
        self.desired["data"] = json.dumps({"actionData": [], "globalVariables": [{"value": 1}]})
        review = self.build()
        self.assertTrue(review["changes"])


if __name__ == "__main__":
    unittest.main()
