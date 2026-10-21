"""Scope-policy migration and independent static/runtime write gates."""
from __future__ import annotations

import copy
import json
import unittest

from tests.helpers import baseline, transaction
from gxp_editor.hashing import sha256_json
from gxp_editor.logic_review import LogicReviewError, build_business_review, normalize_review_spec, require_write_business_review, validate_business_review


class ReviewScopeTests(unittest.TestCase):
    def setUp(self):
        self.canvas = {"actionData": [{"key": "main", "actionType": "csharp", "data": [
            {"key": "query", "elementKey": "GetData", "paramsValue": {"inputParams": {"status": "active"}}},
            {"key": "other", "elementKey": "GetData", "paramsValue": {"outputParams": {"result": "old"}}}]}],
            "globalVariables": []}
        self.base = baseline(data=json.dumps(self.canvas))
        self.candidate = {"data": json.dumps(self.canvas), "js_code": "", "csharp_code": "query", "relation_data": {}}
        self.binding = transaction()["compiler_evidence"]["compiler_binding"]
        self.spec = {"schema_version": 1, "revision": "request-1", "requirements": [
            {"id": "frozen", "text": "只查冻结记录", "source_text": "用户：只改查询状态为freeze",
             "targets": [{"group_key": "main", "node_key": "query", "field_path": "paramsValue/inputParams/status"}]}],
             "scenarios": {"schema_version": 1, "cases": []}}

    def build(self, spec=None):
        return build_business_review(review_spec=self.spec if spec is None else spec, baseline_design=self.base,
                                     candidate_design=self.candidate, compiler_binding=self.binding)

    def update(self, extra=False):
        changed = copy.deepcopy(self.canvas)
        changed["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["status"] = "freeze"
        if extra:
            changed["actionData"][0]["data"][1]["paramsValue"]["outputParams"]["result"] = "wrong"
        self.candidate["data"] = json.dumps(changed)

    @staticmethod
    def rehash(report):
        report["review_sha256"] = sha256_json({key: value for key, value in report.items() if key != "review_sha256"})

    def test_new_specs_default_strict_but_missing_scope_fails_before_compilation(self):
        self.assertEqual(normalize_review_spec(self.spec)["scope_policy"], "strict")
        for spec in (None, {**self.spec, "requirements": []},
                     {**self.spec, "requirements": [{**self.spec["requirements"][0], "targets": []}]},
                     {**self.spec, "requirements": [*self.spec["requirements"], {
                         "id": "missing", "text": "另一项", "source_text": "用户：另一项", "targets": []}]}):
            with self.subTest(spec=spec):
                with self.assertRaisesRegex(LogicReviewError, "每条需求都必须指定 targets"):
                    normalize_review_spec(spec)

    def test_policy_is_a_strict_enum(self):
        for policy in (None, True, "manual", [], {}):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(LogicReviewError, "scope_policy"):
                    normalize_review_spec({**self.spec, "scope_policy": policy})

    def test_strict_outside_change_fails_even_when_target_configuration_passes(self):
        self.update(extra=True)
        self.spec["canvas_assertions"] = [{"id": "status", "requirement_ids": ["frozen"],
            "target": {"group_key": "main", "node_key": "query", "element_key": "GetData"},
            "path": "/inputParams/status", "operator": "equals", "value": "freeze"}]
        review = self.build()
        self.assertEqual(review["canvas_checks"][0]["status"], "passed")
        self.assertEqual(review["status"], "failed")
        self.assertEqual(review["summary"]["scope_failed"], 1)
        self.assertEqual(review["summary"]["scope_unverified"], 0)
        self.assertEqual(review["candidate_gate"]["fail_count"], 1)
        self.assertTrue(review["candidate_gate"]["blocked"])
        failed = next(change for change in review["changes"] if change["status"] == "failed")
        self.assertEqual(failed["node_key"], "other")
        self.assertIn("阻断审批", failed["impact"])
        validate_business_review(review)
        validate_business_review(review, baseline_design=self.base, candidate_design=self.candidate)

    def test_in_scope_csharp_runtime_unknown_does_not_block(self):
        self.update()
        review = self.build()
        self.assertEqual(review["status"], "unverified")
        self.assertEqual(review["summary"]["scope_failed"], 0)
        self.assertFalse(review["candidate_gate"]["blocked"])

    def test_explicit_manual_review_preserves_extra_changes_for_human_review(self):
        self.update(extra=True)
        review = self.build({**self.spec, "scope_policy": "manual_review"})
        self.assertEqual(review["status"], "unverified")
        self.assertEqual(review["summary"]["scope_failed"], 0)
        self.assertEqual(review["summary"]["scope_unverified"], 1)
        self.assertFalse(review["candidate_gate"]["blocked"])
        spec = {"schema_version": 1, "revision": "manual", "scope_policy": "manual_review", "requirements": []}
        self.assertEqual(self.build(spec)["status"], "unverified")

    def test_legacy_missing_policy_stays_readable_without_changing_report(self):
        self.update(extra=True)
        report = self.build({**self.spec, "scope_policy": "manual_review"})
        del report["spec"]["scope_policy"]
        report["bindings"]["review_spec_sha256"] = sha256_json(report["spec"])
        self.rehash(report)
        original = copy.deepcopy(report)
        validate_business_review(report)
        validate_business_review(report, baseline_design=self.base, candidate_design=self.candidate)
        self.assertEqual(report, original)
        self.assertNotIn("scope_policy", normalize_review_spec(report["spec"], legacy_scope=True))
        self.assertEqual(normalize_review_spec(report["spec"])["scope_policy"], "strict")

    def test_legacy_empty_spec_stays_readable_but_cannot_be_reused_as_new_strict_spec(self):
        report = self.build({"schema_version": 1, "revision": "unspecified", "scope_policy": "manual_review", "requirements": []})
        del report["spec"]["scope_policy"]
        report["bindings"]["review_spec_sha256"] = sha256_json(report["spec"])
        self.rehash(report)
        validate_business_review(report)
        with self.assertRaisesRegex(LogicReviewError, "targets"):
            normalize_review_spec(report["spec"])

    def test_legacy_missing_policy_is_readable_but_cannot_execute_an_old_pending_approval(self):
        report = self.build({**self.spec, "scope_policy": "manual_review"})
        artifact = {"kind": "write", "business_review": report, "desired_design": self.candidate,
                    "compiler_evidence": {"hashes": report["bindings"]["candidate_hashes"], "compiler_binding": self.binding},
                    "old_content_hash": self.base["content_hash"]}
        require_write_business_review(artifact)
        del report["spec"]["scope_policy"]
        report["bindings"]["review_spec_sha256"] = sha256_json(report["spec"])
        self.rehash(report)
        validate_business_review(report)
        validate_business_review(report, baseline_design=self.base, candidate_design=self.candidate)
        with self.assertRaisesRegex(LogicReviewError, "旧审批缺少修改范围策略"):
            require_write_business_review(artifact)

    def test_rehashed_status_cannot_hide_an_out_of_scope_change(self):
        self.update(extra=True)
        report = self.build()
        change = next(item for item in report["changes"] if item["status"] == "failed")
        change.update(status="covered", in_scope=True, requirement_ids=["frozen"])
        report["summary"]["scope_failed"] = 0
        report["candidate_gate"].update(blocked=False, fail_count=0)
        self.rehash(report)
        with self.assertRaisesRegex(LogicReviewError, "差异状态与需求目标"):
            validate_business_review(report)
