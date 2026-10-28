"""Actual native mappings and isolated execution; no database or saved drafts."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from tests.helpers import baseline
from gxp_editor.logic_review import LogicReviewError, build_business_review, plan_binding_hash, require_write_business_review
from gxp_editor.logic_scenarios import LogicScenarioRunner

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = Path(os.environ.get("GXP_TEST_FRONTEND_REPO", r"G:\hoyi\updateComponents\gxp2.components"))
BRIDGE = Path(os.environ.get("GXP_TEST_COMPILER_BRIDGE", str(FRONTEND / "dist/action-design-compiler/bridge.js")))


@unittest.skipUnless(shutil.which("node") and BRIDGE.is_file(), "Existing native compiler and parser required")
class NativeExecutionCoverageTests(unittest.TestCase):
    @classmethod
    def native(cls, payload, *, markers=True):
        runner = ROOT / "mcp/gxp_editor/compiler_runner.cjs"
        if markers:
            command = ["node", str(runner), str(BRIDGE)]
        else:
            # Exercise the same real bundle/generators with capture disabled to
            # independently verify that source markers change no saved byte.
            script = """
const fs=require('node:fs'),Module=require('node:module'),path=require('node:path');
const runner=require(process.argv[1]); runner.executionCoverage.markNodeCode=code=>code;
const bridge=process.argv[2],compiler=new Module(bridge,module);
compiler.filename=bridge; compiler.paths=Module._nodeModulePaths(path.dirname(bridge));
compiler._compile(runner.adapt(fs.readFileSync(bridge,'utf8')),bridge);
"""
            command = ["node", "-e", script, str(runner), str(BRIDGE)]
        process = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                                 text=True, encoding="utf-8", cwd=FRONTEND, timeout=20)
        if process.returncode:
            raise AssertionError(process.stdout + process.stderr)
        result = json.loads(process.stdout)
        if not result.get("ok"):
            raise AssertionError(result)
        return result

    @classmethod
    def setUpClass(cls):
        def value(code):
            return {"paramTypes": "custom", "value": code, "code": code, "label": code}
        cls.empty = {"actionData": [{"key": "main", "title": "Main", "actionType": "js", "data": []}], "globalVariables": []}
        operations = [
            {"operation": "append_node", "action_group_key": "main", "element_key": "IfCondition", "node_alias": "gate",
             "input_params": {"condition": {"target": value('inbiz.getPageMode() === "add"')}}},
            {"operation": "insert_in_block", "action_group_key": "main", "target_node_key": "@gate", "element_key": "SetVariableValue",
             "input_params": {"variableName": value('inbiz("result")'), "attributeName": "value",
                              "attributeValue": value('await Promise.resolve("after")')}}]
        cls.compiled = cls.native({"command": "compile_patch", "design": cls.empty, "operations": operations})
        cls.design = cls.compiled["design"]
        cls.mapping = cls.compiled["event_binding_validation"]["execution_coverage"]
        cls.condition_key = cls.design["actionData"][0]["data"][0]["key"]
        cls.assignment_key = cls.design["actionData"][0]["data"][1]["key"]
        cls.binding = {"compiler_binding_sha256": "a" * 64}

    def run_case(self, *, page_mode="add", expected="after", mapping=True, source=None, compiled=None):
        spec = {"schema_version": 1, "cases": [{"id": "case", "requirement_ids": ["rule"],
            "environment": {"page_mode": page_mode, "fields": {"result": {"value": "before"}}},
            "steps": [{"kind": "call_group", "group_key": "main"}], "expect": {"fields": {"result": {"value": expected}}}}]}
        compiled = self.compiled if compiled is None else compiled
        source = compiled["js_code"] if source is None else source
        report = LogicScenarioRunner(SimpleNamespace(frontend_repo=FRONTEND)).run_pair(source, source, spec,
            candidate_coverage=deepcopy(compiled["event_binding_validation"]["execution_coverage"]) if mapping else None,
            compiler_binding=self.binding)
        return spec, report

    def review(self, spec, report, *, node=None, field=None, compiled=None, group="main"):
        compiled = self.compiled if compiled is None else compiled
        target = {"group_key": group, "node_key": node or self.assignment_key}
        if field:
            target["field_path"] = field
        review_spec = {"schema_version": 1, "revision": "frozen-1", "scope_policy": "strict",
            "requirements": [{"id": "rule", "text": "Only add mode writes the field", "source_text": "Only add mode writes the field", "targets": [target]}],
            "scenarios": spec}
        desired = {"data": json.dumps({key:compiled["design"][key] for key in ("actionData", "globalVariables")}, ensure_ascii=False),
                   **{key:compiled[key] for key in ("js_code", "csharp_code", "relation_data")}}
        base = baseline(**{key:desired[key] for key in ("data", "js_code", "csharp_code")})
        return build_business_review(review_spec=review_spec, baseline_design=base, candidate_design=desired,
                                     compiler_binding=self.binding, scenario_report=report)

    def test_mapping_is_independently_rebuilt_and_saved_js_bytes_are_identical(self):
        payload = {"command": "verify_candidate", "design": self.design, "baseline": self.empty,
                   "js_code": self.compiled["js_code"], "generation_mode": "full"}
        verified = self.native(payload)
        self.assertEqual(verified["event_binding_validation"]["execution_coverage"], self.mapping)
        plain = self.native({"command": "compile_design", "design": self.design}, markers=False)
        for field in ("js_code", "csharp_code", "relation_data"):
            self.assertEqual(plain[field], self.compiled[field])
        self.assertNotIn("GXP_COVERAGE_", self.compiled["js_code"])

    def test_real_if_branch_hits_async_assignment_without_changing_api_order(self):
        spec, report = self.run_case()
        _, plain = self.run_case(mapping=False)
        candidate = report["cases"][0]["candidate"]
        self.assertEqual(candidate["status"], "passed", candidate)
        for field in ("actual", "trace", "assertions"):
            self.assertEqual(candidate[field], plain["cases"][0]["candidate"][field])
        hits = {row["node_key"]:row for row in candidate["execution_coverage"]["nodes"]}
        self.assertEqual(hits[self.condition_key]["true_count"], 1)
        self.assertEqual(hits[self.assignment_key]["count"], 1)
        review = self.review(spec, report)
        self.assertEqual(review["requirements"][0]["status"], "passed")
        self.assertFalse(review["runtime_verified"])
        self.assertFalse(review["browser_verified"])

    def test_unhit_node_stays_unverified_and_wrong_branch_fails(self):
        spec, report = self.run_case(page_mode="edit", expected="before")
        hits = {row["node_key"]:row for row in report["cases"][0]["candidate"]["execution_coverage"]["nodes"]}
        self.assertEqual(hits[self.condition_key]["false_count"], 1)
        self.assertEqual(hits[self.assignment_key]["count"], 0)
        self.assertEqual(self.review(spec, report)["requirements"][0]["status"], "unverified")
        self.assertEqual(self.review(spec, report, node=self.condition_key)["requirements"][0]["status"], "passed")
        wrong_spec, wrong = self.run_case(page_mode="edit", expected="after")
        self.assertEqual(self.review(wrong_spec, wrong)["requirements"][0]["status"], "failed")

    def test_fields_and_legacy_unmapped_receipts_remain_unverified(self):
        spec, report = self.run_case()
        self.assertEqual(self.review(spec, report, field="paramsValue/inputParams/attributeValue")["requirements"][0]["status"], "unverified")
        spec, legacy = self.run_case(mapping=False)
        self.assertEqual(self.review(spec, legacy)["requirements"][0]["status"], "unverified")

    def test_native_elseif_counts_and_nested_action_execution(self):
        def value(code):
            return {"paramTypes": "custom", "value": code, "code": code, "label": code}
        operations = [
            {"operation": "insert_in_block", "action_group_key": "main", "target_node_key": self.condition_key,
             "element_key": "ElseIfCondition", "node_alias": "alternate",
             "input_params": {"condition": {"target": value('inbiz.getPageMode() === "edit"')}}},
            {"operation": "insert_after", "action_group_key": "main", "anchor_node_key": "@alternate",
             "element_key": "SetVariableValue", "input_params": {"variableName": value('inbiz("result")'),
                 "attributeName": "value", "attributeValue": value('"alternate"')}}]
        alternate = self.native({"command": "compile_patch", "design": self.design, "operations": operations})
        spec, report = self.run_case(page_mode="edit", expected="alternate", compiled=alternate)
        result = report["cases"][0]["candidate"]
        self.assertEqual(result["status"], "passed", result)
        condition = next(node for node in result["execution_coverage"]["nodes"] if node["element_key"] == "ElseIfCondition")
        self.assertEqual(condition["true_count"], 1)
        self.assertEqual(self.review(spec, report, node=condition["node_key"], compiled=alternate)["requirements"][0]["status"], "passed")

        design = deepcopy(self.empty)
        worker_node = deepcopy(self.design["actionData"][0]["data"][1]); worker_node["depth"] = []
        design["actionData"].append({"key": "worker", "title": "Worker", "actionType": "js", "data": [worker_node]})
        nested = self.native({"command": "compile_patch", "design": design, "operations": [
            {"operation": "append_node", "action_group_key": "main", "element_key": "CallAction",
             "input_params": {"actionName": {"value": "worker", "label": "Worker", "actionType": "js"}}, "output_params": {}}]})
        spec, report = self.run_case(compiled=nested)
        result = report["cases"][0]["candidate"]
        self.assertEqual(result["status"], "passed", result)
        self.assertNotIn("group.worker", [row["api"] for row in result["trace"]])
        self.assertEqual(self.review(spec, report, node=worker_node["key"], group="worker", compiled=nested)["requirements"][0]["status"], "passed")

    def test_changed_source_canvas_compiler_or_counts_cannot_promote_coverage(self):
        spec, report = self.run_case()
        for mutate in (
            lambda r:r["execution_mapping"].__setitem__("source_sha256", "0"*64),
            lambda r:r["execution_mapping"].__setitem__("canvas_sha256", "0"*64),
            lambda r:r.__setitem__("coverage_compiler_sha256", "0"*64),
            lambda r:r["cases"][0]["candidate"]["execution_coverage"]["nodes"][0].__setitem__("true_count", 8),
            lambda r:r["cases"][0]["candidate"].pop("execution_coverage"),
        ):
            altered = deepcopy(report); mutate(altered)
            with self.assertRaises(LogicReviewError):
                self.review(spec, altered)

    def test_plan_and_write_require_the_independently_verified_native_mapping(self):
        from gxp_editor.service import EditorService, EditorServiceError
        spec, report = self.run_case()
        review = self.review(spec, report)
        desired = {"data": json.dumps({key:self.design[key] for key in ("actionData", "globalVariables")}, ensure_ascii=False),
                   **{key:self.compiled[key] for key in ("js_code", "csharp_code", "relation_data")}}
        base = baseline(**{key:desired[key] for key in ("data", "js_code", "csharp_code")})
        compiler = {"compiler_binding":self.binding, "hashes":review["bindings"]["candidate_hashes"],
                    "event_binding_validation":deepcopy(self.compiled["event_binding_validation"])}
        transaction = {"kind":"write", "desired_design":desired, "business_review":review,
                       "compiler_evidence":compiler, "old_content_hash":base["content_hash"]}
        require_write_business_review(transaction)
        artifact = {"business_review":review, "desired":desired, "compiler_binding":self.binding,
                    "base_content_hash":base["content_hash"], "event_binding_validation":compiler["event_binding_validation"]}
        plan = {"new_hash":plan_binding_hash(artifact), "base_hash":base["content_hash"]}
        EditorService._validate_plan_review(plan, {"baseline":base}, artifact)
        compiler["event_binding_validation"]["execution_coverage"]["ranges"][0]["node_key"] = "changed-node"
        with self.assertRaises(LogicReviewError):
            require_write_business_review(transaction)
        with self.assertRaises(EditorServiceError):
            EditorService._validate_plan_review(plan, {"baseline":base}, artifact)


if __name__ == "__main__":
    unittest.main()
