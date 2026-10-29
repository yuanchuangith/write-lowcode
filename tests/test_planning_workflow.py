"""Normal tasks and failed retries across planning, review and approval (no business IO)."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import baseline, editor_config, transaction, runtime_validation
from tests import test_business_review_service as review_service_tests
from tests.test_business_review_service import review_spec, _RUN_PAIR
from tests.test_service import PlanRepository
from gxp_editor.approval import load_artifact, save_artifact
from gxp_editor.compiler import CompilerBridge, CompilerError
from gxp_editor.hashing import compiler_binding_payload, sha256_json
from gxp_editor.logic_review import build_business_review
from gxp_editor.logic_scenarios import LogicScenarioRunner, validate_scenario_spec
from gxp_editor.runtime_validation import RuntimeCsharpValidator
from gxp_editor.service import EditorServiceError


class PlanningReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.flow = review_service_tests.BusinessReviewServiceTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)

    def test_correcting_missing_key_preserves_original_user_evidence(self):
        spec = review_spec()
        spec["requirements"][0]["targets"][0]["node_key"] = "typo"
        self.flow.service._resolve_review_spec(self.flow.service.state.get_session(self.flow.session_id), spec)
        corrected = review_spec()
        corrected["revision"] = "request-2"
        result = self.flow.plan(corrected)
        self.assertTrue(result["ready_for_approval"])
        self.assertEqual(result["review_report"]["spec"]["requirements"][0]["source_text"],
                         spec["requirements"][0]["source_text"])

    def test_existing_target_cannot_be_broadened_without_user_evidence(self):
        self.flow.plan(review_spec())
        changed = review_spec()
        changed["revision"] = "request-2"
        changed["requirements"][0]["targets"] = [{"group_key": "main"}]
        with self.assertRaisesRegex(EditorServiceError, "新的用户依据"):
            self.flow.plan(changed)

    def test_invalid_batch_does_not_checkpoint_unusable_requirements(self):
        with self.assertRaises(CompilerError):
            self.flow.service.plan_canvas_patch(self.flow.session_id, [{"operation": "invalid"}], review_spec=review_spec())
        self.assertIsNone(self.flow.service.state.review_spec_checkpoint(self.flow.session_id))
        self.assertTrue(self.flow.plan(review_spec())["ready_for_approval"])

    def test_append_combines_batches_and_rejects_stale_plan_ids_or_tampering(self):
        first = self.flow.plan(review_spec())
        appended = self.flow.service.plan_canvas_patch(self.flow.session_id, self.flow.operations,
                                                      append_to_plan_id=first["plan_id"])
        self.assertEqual(appended["operation_count"], 2)
        self.assertEqual(self.flow.compiler.compile_patch.call_args.args[1], self.flow.operations * 2)
        with self.assertRaisesRegex(EditorServiceError, "最新画布计划"):
            self.flow.service.plan_canvas_patch(self.flow.session_id, self.flow.operations,
                                                append_to_plan_id=first["plan_id"])
        plan = self.flow.service.state.get_plan(appended["plan_id"])
        artifact = load_artifact(plan["artifact_path"])
        artifact["operations"].clear()
        save_artifact(plan["artifact_path"], artifact)
        with self.assertRaisesRegex(EditorServiceError, "计划或业务验收报告已变化"):
            self.flow.service.plan_canvas_patch(self.flow.session_id, self.flow.operations,
                                                append_to_plan_id=appended["plan_id"])

    def test_new_node_value_assertion_passes_but_unchanged_still_fails(self):
        before = {"actionData": [{"key": "main", "actionType": "js", "data": []}], "globalVariables": []}
        after = copy.deepcopy(before)
        after["actionData"][0]["data"] = [{"key": "new", "elementKey": "SetField",
            "paramsValue": {"inputParams": {"status": "freeze"}, "outputParams": {}}}]
        spec = {"schema_version": 1, "revision": "new", "requirements": [{"id": "r", "text": "Add field",
            "source_text": "Add field with freeze status", "targets": [{"group_key": "main", "node_key": "new"}]}],
            "canvas_assertions": [{"id": "a", "requirement_ids": ["r"], "target": {
                "group_key": "main", "node_key": "new", "element_key": "SetField"},
                "path": "/inputParams/status", "operator": "equals", "value": "freeze"}]}
        def review():
            return build_business_review(review_spec=spec, baseline_design=baseline(data=json.dumps(before)),
                candidate_design={"data": json.dumps(after), "js_code": "", "csharp_code": "", "relation_data": {}},
                compiler_binding=transaction()["compiler_evidence"]["compiler_binding"])
        self.assertEqual(review()["canvas_checks"][0]["status"], "passed")
        before = copy.deepcopy(after)
        before["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["status"] = "active"
        for prefix in ("", "/"):
            spec["requirements"][0]["targets"][0]["field_path"] = prefix + "paramsValue/inputParams/status"
            report = review()
            self.assertEqual(report["canvas_checks"][0]["status"], "passed")
            self.assertTrue(all(change["in_scope"] for change in report["changes"]))
        before["actionData"][0]["data"].clear()
        spec["requirements"][0]["targets"][0].pop("field_path")
        spec["canvas_assertions"][0].update(operator="unchanged")
        del spec["canvas_assertions"][0]["value"]
        self.assertEqual(review()["canvas_checks"][0]["status"], "failed")

    def test_scenario_size_uses_bytes_not_case_or_step_count(self):
        spec = {"schema_version": 1, "cases": [{"id": f"case{i}",
            "steps": [{"kind": "read_metadata"}] * 11} for i in range(21)]}
        self.assertEqual(validate_scenario_spec(spec), spec)

    def test_approval_reuses_generation_and_successful_build(self):
        result = self.flow.plan(review_spec())
        self.flow.compiler.verify_candidate_artifact.reset_mock()
        runtime = Mock()
        runtime.reuse_evidence.return_value = {**runtime_validation(""), "build_reused": True}
        components = self.flow.service._components()
        with patch.object(self.flow.service, "_components", return_value=(*components[:4], runtime)), \
                patch.object(self.flow.service, "_runtime_validation_evidence") as build:
            pending = self.flow.service.prepare_write(result["plan_id"])
        self.assertEqual(pending["state"], "awaiting_approval")
        self.flow.compiler.verify_candidate_artifact.assert_not_called()
        build.assert_not_called()


class CompilerInputReuseTests(unittest.TestCase):
    def test_unrelated_git_metadata_does_not_change_binding_but_sources_do(self):
        binding = transaction()["compiler_evidence"]["compiler_binding"]
        binding["source_provenance"].update(binding_scope="compiler_inputs_v1")
        first = compiler_binding_payload(binding)
        changed = copy.deepcopy(binding)
        changed["source_provenance"].update(commit="f" * 40, branch="documentation", workspace_sha256="f" * 64)
        self.assertEqual(first, compiler_binding_payload(changed))
        changed["rule_source_sha256"] = "0" * 64
        self.assertNotEqual(first, compiler_binding_payload(changed))
        binding = first
        binding["compiler_binding_sha256"] = sha256_json(first)
        bridge = CompilerBridge(editor_config())
        binding["source_provenance"].update(frontend_repo=str(bridge.frontend_repo), bridge_path="bridge.js")
        binding["compiler_binding_sha256"] = sha256_json(compiler_binding_payload(binding))
        key = ("repo", "bridge", binding["bridge_sha256"], binding["adapter_sha256"], binding["rule_source_sha256"])
        with patch.object(bridge, "_source_key", return_value=key), patch.object(bridge, "compiler_binding") as catalog:
            self.assertEqual(bridge.verify_binding(binding), binding)
            catalog.assert_not_called()
        with patch.object(bridge, "_source_key", return_value=(*key[:4], "0" * 64)):
            with self.assertRaisesRegex(CompilerError, "source changed"):
                bridge.verify_binding(binding)

    def test_runtime_build_reuse_rejects_changed_reference_bytes(self):
        from gxp_editor.hashing import sha256_text
        runtime = RuntimeCsharpValidator(editor_config())
        evidence = {"available": True, "success": True, "compilation_succeeded": True,
                    "csharp_sha256": sha256_text("code"), "reference_manifest_sha256": "a" * 64}
        with patch.object(runtime, "_references", return_value=[]), \
                patch.object(runtime, "_manifest", return_value={"reference_manifest_sha256": "a" * 64}):
            self.assertTrue(runtime.reuse_evidence(evidence, "code")["build_reused"])
            self.assertIsNone(runtime.reuse_evidence(evidence, "changed code"))
        with patch.object(runtime, "_references", return_value=[]), \
                patch.object(runtime, "_manifest", return_value={"reference_manifest_sha256": "b" * 64}):
            self.assertIsNone(runtime.reuse_evidence(evidence, "code"))

    def test_same_native_result_is_not_generated_twice_and_mutation_is_rejected(self):
        from tests.test_compiler import CompilerBridgeTests
        with tempfile.TemporaryDirectory() as folder:
            bridge = CompilerBridgeTests().bridge(Path(folder))
            design = {"actionData": [], "globalVariables": []}
            binding = transaction()["compiler_evidence"]["compiler_binding"]
            result = {"design": design, "js_code": "", "csharp_code": "", "relation_data": {},
                      "compiler_binding": binding, "validation": {"valid": True},
                      "event_binding_validation": {"binding_node_keys": []},
                      "_generation_context": {"baseline_design": design, "baseline_artifacts": None, "generation_mode": "full"}}
            bridge._compiled_result = result
            bridge._compiled_result_sha256 = bridge._compiled_fingerprint(result)
            with patch.object(bridge, "_verify_candidate", side_effect=AssertionError("Unchanged native output must be reused")):
                self.assertTrue(bridge.verify_compiled_result(result)["generated_code_consistency_verified"])
                result["js_code"] = "tampered"
                with self.assertRaisesRegex(CompilerError, "evidence changed"):
                    bridge.verify_compiled_result(result)


FRONTEND = Path(os.environ.get("GXP_TEST_FRONTEND_REPO", r"G:\hoyi\updateComponents\gxp2.components"))
NATIVE_BRIDGE = FRONTEND / "dist/action-design-compiler/bridge.js"


@unittest.skipUnless(NATIVE_BRIDGE.is_file(), "Existing native compiler is required")
class NativePlanningWorkflowTests(unittest.TestCase):
    def test_large_new_group_append_failed_retry_and_single_approval(self):
        flow = review_service_tests.BusinessReviewServiceTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        config = replace(editor_config(), frontend_repo=str(FRONTEND), compiler_bridge=str(NATIVE_BRIDGE))
        compiler = CompilerBridge(config)
        canvas = {"actionData": [{"key": "main", "title": "Main", "actionType": "js", "data": []}], "globalVariables": []}
        generated = compiler._call({"command": "compile_design", "design": {**canvas, "code": "ActionOne"}})
        draft = baseline(data=json.dumps(canvas), data_json=canvas, js_code=generated["js_code"], csharp_code=generated["csharp_code"], relations=[])
        flow.service.state.update_session(flow.session_id, "aborted")
        session = flow.service.state.create_session(draft, "offline-backup", compiler.compiler_binding()["compiler_binding_sha256"])
        repository = PlanRepository(draft)
        runner = LogicScenarioRunner(config)
        flow.runner.side_effect = lambda old, new, spec, **kwargs: _RUN_PAIR(runner, old, new, spec, **kwargs)

        def variable(index):
            expression = json.dumps(str(index))
            def custom(value):
                return {"paramTypes": "custom", "value": value, "code": value, "label": value, "dataType": "string"}
            return {"operation": "append_node", "action_group_key": "@child", "node_alias": f"v{index}",
                    "element_key": "SetVariable", "input_params": {"variableType": "string", "variableValue": custom(expression)},
                    "output_params": {"variableName": custom(f"value{index}")}}

        operations = [{"operation": "create_action_group", "group_alias": "child", "group_title": "Child", "action_type": "js"}]
        operations += [variable(index) for index in range(101)]
        spec = {"schema_version": 1, "revision": "original-request", "requirements": [{"id": "create", "text": "Create child values",
            "source_text": "Create child values in batches", "targets": [{"group_key": "@child"}]}],
            "canvas_assertions": [{"id": "first", "requirement_ids": ["create"], "target": {"group_key": "@child",
                "node_key": "@v0", "element_key": "SetVariable"}, "path": "/inputParams/variableValue",
                "operator": "equals", "value": variable(0)["input_params"]["variableValue"]}],
            "scenarios": {"schema_version": 1, "cases": [{"id": f"case{i}", "requirement_ids": ["create"],
                "steps": [{"kind": "call_group", "group_key": "@child"}] * 11,
                "expect": {"call_counts": {"group.@child": 11}}} for i in range(21)]}}
        with patch.object(flow.service, "_components", return_value=(config, object(), repository, compiler, RuntimeCsharpValidator(config))):
            first = flow.service.plan_canvas_patch(session["id"], operations, review_spec=spec)
            self.assertTrue(first["ready_for_approval"], first.get("review_issues"))
            self.assertEqual(first["operation_count"], 102)
            self.assertEqual(first["review_report"]["canvas_checks"][0]["status"], "passed")
            self.assertTrue(all(case["candidate"]["status"] == "passed" for case in first["review_report"]["scenarios"]["cases"]))
            with self.assertRaises(CompilerError):
                flow.service.plan_canvas_patch(session["id"], [{"operation": "invalid"}], append_to_plan_id=first["plan_id"])
            appended = flow.service.plan_canvas_patch(session["id"], [variable(101)], append_to_plan_id=first["plan_id"])
            self.assertTrue(appended["ready_for_approval"])
            self.assertEqual(appended["operation_count"], 103)
            with patch.object(compiler, "verify_candidate_artifact", side_effect=AssertionError("Approval must not regenerate")), \
                    patch.object(compiler, "_git", side_effect=AssertionError("Approval must not launch Git")), \
                    patch.object(compiler, "_node_identity", side_effect=AssertionError("Approval must not launch Node")):
                started = time.perf_counter()
                pending = flow.service.prepare_write(appended["plan_id"])
                elapsed = time.perf_counter() - started
            self.assertEqual(pending["statement_count"], 1)
            receipt = load_artifact(flow.service.state.get_transaction(pending["transaction_id"])["artifact_path"])
            self.assertEqual(receipt["business_review"]["source_spec"]["revision"], "original-request")
            self.assertEqual(len(receipt["business_review"]["spec"]["requirements"]), 1)
            print(f"native_workflow operations=103 cases=21 steps=11 approval_prepare_ms={elapsed * 1000:.1f}")
