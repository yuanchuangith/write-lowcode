"""Requirements cannot borrow passing cases from unexecuted target groups."""
from __future__ import annotations

import copy
import json
import unittest

from tests.helpers import baseline, transaction
from gxp_editor.hashing import sha256_json, sha256_text
from gxp_editor.logic_review import build_business_review, validate_business_review, LogicReviewError


class RequirementTargetTests(unittest.TestCase):
    def setUp(self):
        self.canvas = {"actionData": [
            {"key": "main", "actionType": "js", "data": [{"key": "assign", "paramsValue": {"value": "old"}}]},
            {"key": "query", "actionType": "csharp", "data": [{"key": "select", "elementKey": "SelectData",
                "paramsValue": {"inputParams": {"filter": "status=freeze"}}}]},
        ], "globalVariables": []}
        self.source = "function(){return {main:async function(){return true;}}}"
        self.base = baseline(data=json.dumps(self.canvas), js_code=self.source)
        after = copy.deepcopy(self.canvas)
        after["actionData"][1]["data"][0]["paramsValue"]["inputParams"]["filter"] = "status=active"
        self.candidate = {"data": json.dumps(after), "js_code": self.source, "csharp_code": "candidate", "relation_data": {}}
        self.binding = transaction()["compiler_evidence"]["compiler_binding"]

    def build(self, targets, *, trace=None, extra_requirement=None, outcome="passed"):
        requirements = [{"id": "rule", "text": "Only frozen rows", "source_text": "User: only status freeze rows", "targets": targets}]
        if extra_requirement:
            requirements.append(extra_requirement)
        spec = {"schema_version": 1, "revision": "request-1", "requirements": requirements,
                "scenarios": {"schema_version": 1, "cases": [{"id": "case", "requirement_ids": [r["id"] for r in requirements],
                    "steps": [{"kind": "call_group", "group_key": "main"}], "expect": {"last_return": True}}]}}
        report = {"schema_version": 1, "runner_version": "1", "spec_sha256": sha256_json(spec["scenarios"]),
                  "baseline_source_sha256": sha256_text(self.source), "candidate_source_sha256": sha256_text(self.source),
                  "cases": [{"id": "case", "requirement_ids": [r["id"] for r in requirements], "expected": {"last_return": True},
                      "baseline": {"status": "passed"}, "candidate": {"status": outcome,
                          "trace": [{"api": "group.main"}] if trace is None else trace}}]}
        return build_business_review(review_spec=spec, baseline_design=self.base, candidate_design=self.candidate,
                                     compiler_binding=self.binding, scenario_report=report)

    def test_csharp_target_stays_unverified_despite_passing_js_case(self):
        review = self.build([{"group_key": "query", "node_key": "select"}])
        self.assertEqual(review["scenarios"]["cases"][0]["candidate"]["status"], "passed")
        self.assertEqual(review["requirements"][0]["status"], "unverified")
        self.assertIn("C#", review["requirements"][0]["reason"])
        self.assertEqual(review["status"], "unverified")
        self.assertFalse(review["candidate_gate"]["blocked"])
        validate_business_review(review)

    def test_unknown_group_node_type_and_missing_targets_stay_unverified(self):
        for targets in ([], [{"group_key": "missing"}], [{"group_key": "main", "node_key": "missing"}]):
            with self.subTest(targets=targets):
                self.assertEqual(self.build(targets)["requirements"][0]["status"], "unverified")
        self.candidate["data"] = json.dumps({"actionData": [{"key": "unknown", "data": []}], "globalVariables": []})
        self.assertEqual(self.build([{"group_key": "unknown"}])["requirements"][0]["status"], "unverified")

    def test_js_requirement_in_mixed_canvas_can_pass_independently(self):
        js = {"id": "js-rule", "text": "Initialization returns true", "source_text": "User: initialization returns true",
              "targets": [{"group_key": "main"}]}
        review = self.build([{"group_key": "query", "node_key": "select"}], extra_requirement=js)
        self.assertEqual([r["status"] for r in review["requirements"]], ["unverified", "passed"])

    def test_unrelated_js_trace_cannot_prove_target_group(self):
        review = self.build([{"group_key": "main", "node_key": "assign"}], trace=[{"api": "group.other"}])
        self.assertEqual(review["requirements"][0]["status"], "unverified")
        self.assertIn("目标分组", review["requirements"][0]["reason"])

    def test_group_trace_cannot_certify_a_node_in_a_skipped_branch(self):
        review = self.build([{"group_key": "main", "node_key": "assign"}])
        self.assertEqual(review["requirements"][0]["status"], "unverified")
        self.assertIn("节点未验证", review["requirements"][0]["reason"])
        self.assertEqual(review["scenarios"]["cases"][0]["candidate"]["status"], "passed")

    def test_group_trace_cannot_certify_a_field_without_node_key(self):
        review = self.build([{"group_key": "main", "field_path": "inputParams"}])
        self.assertEqual(review["requirements"][0]["status"], "unverified")
        self.assertIn("字段未验证", review["requirements"][0]["reason"])

    def test_event_and_registration_trace_do_not_prove_callback_group_identity(self):
        callback = {"key": "callback", "actionType": "js", "data": [{"key": "callback-node"}]}
        binding = {"key": "binding", "elementKey": "ComponentEvent", "paramsValue": {"inputParams": {
            "name": {"paramTypes": "componentsVariable", "value": "componentsVariable-table"},
            "eventName": "click", "action": {"value": "callback"}}}}
        after = json.loads(self.candidate["data"])
        after["actionData"][0]["data"].append(binding)
        after["actionData"].append(callback)
        self.candidate["data"] = json.dumps(after)
        event_trace = [{"api": "group.main"}, {"api": "table.configEvent", "event": "click"}, {"api": "event.table.click"}]
        review = self.build([{"group_key": "callback", "node_key": "callback-node"}], trace=event_trace)
        self.assertEqual(review["requirements"][0]["status"], "unverified")
        self.assertEqual(review["scenarios"]["cases"][0]["candidate"]["status"], "passed")
        self.assertIn("目标分组", review["requirements"][0]["reason"])
        ambiguous = copy.deepcopy(binding)
        ambiguous["key"] = "other-binding"
        ambiguous["paramsValue"]["inputParams"]["action"]["value"] = "main"
        after["actionData"][0]["data"].append(ambiguous)
        self.candidate["data"] = json.dumps(after)
        review = self.build([{"group_key": "callback"}], trace=event_trace)
        self.assertEqual(review["requirements"][0]["status"], "unverified")

    def test_other_native_inputs_and_missing_trace_do_not_break_review(self):
        after = json.loads(self.candidate["data"])
        after["actionData"][0]["data"].append({"key": "log", "elementKey": "OtherElement",
            "paramsValue": {"inputParams": {"name": "plain text"}}})
        self.candidate["data"] = json.dumps(after)
        self.assertEqual(self.build([{"group_key": "main"}])["requirements"][0]["status"], "passed")
        self.assertEqual(self.build([{"group_key": "main"}], trace=[])["requirements"][0]["status"], "unverified")

    def test_write_validation_rejects_old_green_csharp_requirement(self):
        review = self.build([{"group_key": "query"}])
        review["requirements"][0].update(status="passed", reason="关联场景均通过；覆盖范围限于这些场景")
        review["summary"].update(requirements_passed=1, requirements_unverified=0)
        review["candidate_gate"]["unknown_count"] -= 1
        review["status"] = "passed"
        review["review_sha256"] = sha256_json({k: v for k, v in review.items() if k != "review_sha256"})
        with self.assertRaisesRegex(LogicReviewError, "逐条需求状态"):
            validate_business_review(review, candidate_design=self.candidate)

    def test_known_failure_remains_blocking_even_if_target_is_csharp(self):
        review = self.build([{"group_key": "query"}], outcome="failed")
        self.assertEqual(review["requirements"][0]["status"], "failed")
        self.assertTrue(review["candidate_gate"]["blocked"])
