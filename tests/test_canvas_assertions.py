from __future__ import annotations

import copy
import json
import unittest

from tests.helpers import baseline, transaction
from gxp_editor.canvas_assertions import build_canvas_checks
from gxp_editor.hashing import sha256_json
from gxp_editor.logic_review import LogicReviewError, build_business_review, normalize_review_spec, require_write_business_review, validate_business_review


def specification(operator="equals", value="freeze", path="/inputParams/status"):
    assertion = {"id": "status", "requirement_ids": ["filter"],
                 "target": {"group_key": "main", "node_key": "query", "element_key": "GetData"},
                 "path": path, "operator": operator}
    if operator != "unchanged":
        assertion["value"] = value
    return {"schema_version": 1, "revision": "request-1",
            "requirements": [{"id": "filter", "text": "只查冻结记录", "source_text": "用户：只查冻结记录，保留输出",
                              "targets": [{"group_key": "main", "node_key": "query"}]}],
            "scenarios": {"schema_version": 1, "cases": []}, "canvas_assertions": [assertion]}


class CanvasAssertionTests(unittest.TestCase):
    def setUp(self):
        self.canvas = {"actionData": [{"key": "main", "actionType": "csharp", "data": [
            {"key": "query", "elementKey": "GetData", "paramsValue": {
                "inputParams": {"status": "freeze", "filter": [{"field": "isDeleted", "value": 0}], "nullable": None},
                "outputParams": {"result": "qualifications"}}}]}], "globalVariables": []}
        self.base = baseline(data=json.dumps(self.canvas))
        self.desired = {"data": json.dumps(self.canvas), "js_code": "new-js", "csharp_code": "new-csharp", "relation_data": {}}
        self.binding = transaction()["compiler_evidence"]["compiler_binding"]

    def build(self, spec=None):
        return build_business_review(review_spec=spec or specification(), baseline_design=self.base,
                                     candidate_design=self.desired, compiler_binding=self.binding)

    @staticmethod
    def rehash(review):
        review["review_sha256"] = sha256_json({key: value for key, value in review.items() if key != "review_sha256"})

    def update_candidate(self, mutate):
        canvas = copy.deepcopy(self.canvas)
        mutate(canvas)
        self.desired["data"] = json.dumps(canvas)

    def test_configuration_pass_does_not_claim_csharp_business_execution(self):
        spec = specification()
        spec["requirements"][0]["targets"][0]["field_path"] = "paramsValue/inputParams/status"
        review = self.build(spec)
        self.assertEqual(review["canvas_checks"][0]["status"], "passed")
        self.assertEqual(review["requirements"][0]["configuration_status"], "passed")
        self.assertEqual(review["requirements"][0]["configuration_check_ids"], ["status"])
        self.assertEqual(review["requirements"][0]["status"], "unverified")
        self.assertEqual(review["status"], "unverified")
        self.assertFalse(review["candidate_gate"]["blocked"])
        self.assertEqual(review["summary"]["configuration_passed"], 1)
        validate_business_review(review)
        validate_business_review(review, candidate_design=self.desired)

    def test_broad_or_partly_covered_requirement_never_claims_full_configuration(self):
        for targets in ([{"group_key": "main"}], [{"group_key": "main", "node_key": "query"}],
                        [{"group_key": "main", "node_key": "query", "field_path": "paramsValue/inputParams/status"},
                         {"group_key": "main", "node_key": "query", "field_path": "paramsValue/outputParams/result"}]):
            with self.subTest(targets=targets):
                spec = specification()
                spec["requirements"][0]["targets"] = targets
                review = self.build(spec)
                requirement = review["requirements"][0]
                self.assertEqual(requirement["configuration_status"], "unverified")
                self.assertIn("范围尚未完整核对", requirement["configuration_reason"])
                self.assertEqual(review["canvas_checks"][0]["status"], "passed")
                validate_business_review(review)

    def test_field_coverage_requires_equal_or_unchanged_at_same_or_parent_path(self):
        for operator, path, value, expected_status in (
            ("equals", "/inputParams/status", "freeze", "passed"),
            ("unchanged", "/inputParams", None, "passed"),
            ("contains", "/inputParams/filter", {"field": "isDeleted", "value": 0}, "unverified")):
            with self.subTest(operator=operator):
                spec = specification(operator, value, path)
                target_path = "paramsValue/inputParams/status" if operator != "contains" else "paramsValue/inputParams/filter"
                spec["requirements"][0]["targets"][0]["field_path"] = target_path
                review = self.build(spec)
                self.assertEqual(review["canvas_checks"][0]["status"], "passed")
                self.assertEqual(review["requirements"][0]["configuration_status"], expected_status)

    def test_failed_configuration_blocks_without_any_runtime_scenarios(self):
        review = self.build(specification(value="active"))
        self.assertEqual(review["status"], "failed")
        self.assertEqual(review["candidate_gate"]["fail_count"], 1)
        self.assertTrue(review["candidate_gate"]["blocked"])
        self.assertEqual(review["requirements"][0]["configuration_status"], "failed")
        artifact = {"kind": "write", "business_review": review, "desired_design": self.desired,
                    "compiler_evidence": {"hashes": review["bindings"]["candidate_hashes"], "compiler_binding": self.binding},
                    "old_content_hash": self.base["content_hash"]}
        with self.assertRaisesRegex(LogicReviewError, "失败"):
            require_write_business_review(artifact)

    def test_write_rechecks_candidate_and_rejects_missing_candidate(self):
        review = self.build()
        artifact = {"kind": "write", "business_review": review, "desired_design": self.desired,
                    "compiler_evidence": {"hashes": review["bindings"]["candidate_hashes"], "compiler_binding": self.binding},
                    "old_content_hash": self.base["content_hash"]}
        require_write_business_review(artifact)
        del artifact["desired_design"]
        with self.assertRaisesRegex(LogicReviewError, "候选"):
            require_write_business_review(artifact)

    def test_contains_requires_exact_typed_array_member(self):
        for value, passed in (({"field": "isDeleted", "value": 0}, True),
                              ({"field": "isDeleted", "value": False}, False),
                              ({"field": "isDeleted"}, False)):
            with self.subTest(value=value):
                review = self.build(specification("contains", value, "/inputParams/filter"))
                self.assertEqual(review["canvas_checks"][0]["status"], "passed" if passed else "failed")
        review = self.build(specification("contains", "free", "/inputParams/status"))
        self.assertEqual(review["canvas_checks"][0]["status"], "failed")

    def test_null_is_distinct_from_missing_and_boolean_from_number(self):
        review = self.build(specification(value=None, path="/inputParams/nullable"))
        self.assertEqual(review["canvas_checks"][0]["status"], "passed")
        review = self.build(specification(value=None, path="/inputParams/missing"))
        self.assertEqual(review["canvas_checks"][0]["status"], "failed")
        self.assertFalse(review["canvas_checks"][0]["after_present"])
        for value in (True, 0.0, "0"):
            review = self.build(specification(value=value, path="/inputParams/filter/0/value"))
            self.assertEqual(review["canvas_checks"][0]["status"], "failed")

    def test_unchanged_detects_output_regression_and_rechecks_bound_before_values(self):
        spec = specification("unchanged", path="/outputParams")
        review = self.build(spec)
        self.assertEqual(review["canvas_checks"][0]["status"], "passed")
        validate_business_review(review, candidate_design=self.desired)
        self.update_candidate(lambda c: c["actionData"][0]["data"][0]["paramsValue"]["outputParams"].update(result="wrong"))
        with self.assertRaises(LogicReviewError):
            validate_business_review(review, candidate_design=self.desired)
        failed = self.build(spec)
        self.assertEqual(failed["canvas_checks"][0]["status"], "failed")
        assertion = normalize_review_spec(spec)["canvas_assertions"]
        missing_baseline = build_canvas_checks(assertion, candidate=self.canvas)
        self.assertEqual(missing_baseline[0]["status"], "unverified")

    def test_missing_node_changed_type_or_code_type_never_passes(self):
        mutations = (lambda c: c["actionData"][0]["data"].clear(),
                     lambda c: c["actionData"][0]["data"][0].update(elementKey="SetField"),
                     lambda c: c["actionData"][0].update(actionType="js"))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.update_candidate(mutate)
                review = self.build()
                self.assertEqual(review["canvas_checks"][0]["status"], "failed")

    def test_missing_baseline_path_fails_unchanged_but_allows_expected_new_value(self):
        old = copy.deepcopy(self.canvas)
        del old["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["status"]
        self.base = baseline(data=json.dumps(old))
        self.assertEqual(self.build()["canvas_checks"][0]["status"], "passed")
        review = self.build(specification("unchanged"))
        self.assertEqual(review["canvas_checks"][0]["status"], "failed")

    def test_json_pointer_escapes_array_indices_and_scope_are_exact(self):
        canvas = copy.deepcopy(self.canvas)
        canvas["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["a/b~c"] = "freeze"
        self.base = baseline(data=json.dumps(canvas))
        self.desired["data"] = json.dumps(canvas)
        spec = specification(path="/inputParams/a~1b~0c")
        spec["requirements"][0]["targets"][0]["field_path"] = "paramsValue/inputParams"
        self.assertEqual(self.build(spec)["canvas_checks"][0]["status"], "passed")
        spec["requirements"][0]["targets"][0]["field_path"] = "paramsValue/outputParams"
        with self.assertRaisesRegex(LogicReviewError, "范围"):
            normalize_review_spec(spec)

    def test_invalid_schema_is_rejected_before_generation(self):
        mutations = [lambda s: s["canvas_assertions"][0].update(extra=True),
                     lambda s: s["canvas_assertions"][0].update(requirement_ids=["unknown"]),
                     lambda s: s["canvas_assertions"][0].update(requirement_ids=["filter", "filter"]),
                     lambda s: s["canvas_assertions"][0].update(target={"group_key": "main", "node_key": "query"}),
                     lambda s: s["canvas_assertions"][0]["target"].update(node_key="other"),
                     lambda s: s["canvas_assertions"][0].update(path="/elementKey"),
                     lambda s: s["canvas_assertions"][0].update(path="/inputParams/~2"),
                     lambda s: s["canvas_assertions"][0].update(operator="script"),
                     lambda s: s["canvas_assertions"][0].update(operator="unchanged"),
                     lambda s: s["canvas_assertions"][0].pop("value"),
                     lambda s: s["canvas_assertions"][0].update(value=float("nan")),
                     lambda s: s["canvas_assertions"].extend([copy.deepcopy(s["canvas_assertions"][0])] * 100),
                     lambda s: s["canvas_assertions"][0].update(value="x" * (2 * 1024 * 1024))]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                spec = specification()
                mutate(spec)
                with self.assertRaises(LogicReviewError):
                    normalize_review_spec(spec)

    def test_nonfinite_nested_and_nonjson_values_are_rejected(self):
        deep = None
        for _ in range(65):
            deep = [deep]
        for value in (float("inf"), float("-inf"), deep, {1: "not-a-string-key"}, {"set"}):
            with self.subTest(value_type=type(value)):
                spec = specification(value=value)
                with self.assertRaises(LogicReviewError):
                    normalize_review_spec(spec)

    def test_tampered_status_expected_values_scope_and_missing_checks_are_rejected(self):
        for mutate in (lambda r: r["canvas_checks"][0].update(status="failed"),
                       lambda r: r["canvas_checks"][0].update(expected="other"),
                       lambda r: r["canvas_checks"][0].update(after=True),
                       lambda r: r["canvas_checks"][0]["target"].update(node_key="other"),
                       lambda r: r.pop("canvas_checks")):
            with self.subTest(mutate=mutate):
                review = self.build()
                mutate(review)
                self.rehash(review)
                with self.assertRaises(LogicReviewError):
                    validate_business_review(review)

    def test_true_candidate_values_override_rehashed_recorded_values(self):
        review = self.build(specification(value="active"))
        check = review["canvas_checks"][0]
        check.update(after="active", status="passed", reason="配置与固定断言一致；不代表运行验证通过")
        self.rehash(review)
        with self.assertRaisesRegex(LogicReviewError, "真实画布"):
            validate_business_review(review, candidate_design=self.desired)

    def test_legacy_spec_does_not_gain_new_report_fields(self):
        spec = specification()
        del spec["canvas_assertions"]
        review = self.build(spec)
        self.assertNotIn("canvas_checks", review)
        self.assertNotIn("configuration_status", review["requirements"][0])
        self.assertNotIn("configuration_passed", review["summary"])
        validate_business_review(review)
