from __future__ import annotations

from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import baseline, editor_config
from tests.test_service import FakeCompiler, UnavailableRuntimeValidator
from gxp_editor.reference_repair import ReferenceRepairError, build_repair, inspection_projection, scan_references
from gxp_editor.service import EditorService, EditorServiceError
from gxp_editor.state import StateStore
from gxp_editor.compiler import CompilerBridge, CompilerError
from gxp_editor.approval_display import reference_repair_summary


REGISTRY = {"elements": [{"key": "SetVariable"}, {"key": "If", "level_marker": True}, {"key": "IfEnd", "end_marker": True}]}


def reference(name="source", *, kind="inputParam", dtype="string", value="obsolete-key"):
    return {"paramTypes": kind, "value": value, "label": "输入参数-" + name,
            "code": name, "dataType": dtype}


def node(key, value, name=None, *, depth=None):
    name = name or "result" + key
    return {"key": key, "elementKey": "SetVariable", "depth": depth or [],
            "paramsValue": {"inputParams": {"variableType": "string", "variableValue": value},
                            "outputParams": {"variableName": {"paramTypes": "custom", "value": name,
                                                              "code": name, "label": name, "dataType": "string"}}}}


def design(nodes=None):
    return {"actionData": [{"key": "main", "title": "主动作", "actionType": "js",
                            "inputParams": [{"key": "current-key", "title": "source", "type": "string"}],
                            "outputParams": [], "data": nodes or [node("one", reference())]}],
            "globalVariables": []}


class ReferenceRepairTests(unittest.TestCase):
    def test_thirty_bindings_repaired_in_one_batch_without_mutating_original(self):
        original = design([node(str(i), reference()) for i in range(30)])
        before = deepcopy(original)
        report = scan_references(original, REGISTRY)
        self.assertEqual(report["summary"], {"total": 30, "automatic": 30, "ambiguous": 0, "unresolved": 0})
        repaired = build_repair(original, report, [])
        self.assertEqual(len(repaired["repairs"]), 30)
        self.assertEqual(scan_references(repaired["design"], REGISTRY)["summary"]["total"], 0)
        self.assertEqual(original, before)
        self.assertTrue(all(item["after"]["value"] == "current-key" for item in repaired["repairs"]))

    def test_old_source_is_a_previous_local_output_not_an_input(self):
        declaration = node("declare", {"paramTypes": "custom", "value": "''", "code": "''"}, "oldSource")
        original = design([declaration, node("use", reference("oldSource"))])
        report = scan_references(original, REGISTRY)
        repaired = build_repair(original, report, [])
        binding = repaired["repairs"][0]
        self.assertEqual(binding["after"]["paramTypes"], "localVariable")
        self.assertEqual(binding["after"]["value"], "localVariable-oldSource")
        self.assertEqual(binding["source"]["node_key"], "declare")

    def test_same_name_input_and_local_requires_explicit_candidate_choice(self):
        declaration = node("declare", {"paramTypes": "custom", "value": "''", "code": "''"}, "source")
        original = design([declaration, node("use", reference())])
        report = scan_references(original, REGISTRY)
        self.assertEqual(report["summary"]["ambiguous"], 1)
        self.assertTrue(build_repair(original, report, [])["blocked"])
        issue = report["issues"][0]
        local = next(c for c in issue["candidates"] if c["source"]["kind"] == "localVariable")
        choice = {"repair_id": issue["repair_id"], "candidate_id": local["candidate_id"]}
        self.assertFalse(build_repair(original, report, [choice])["blocked"])
        with self.assertRaises(ReferenceRepairError):
            build_repair(original, report, [{**choice, "candidate_id": "not-a-visible-source"}])

    def test_type_mismatch_and_future_declaration_never_match(self):
        original = design([node("use", reference("later")), node("declare", {"paramTypes": "custom", "value": "''", "code": "''"}, "later")])
        self.assertEqual(scan_references(original, REGISTRY)["summary"]["unresolved"], 1)
        original = design([node("use", reference(dtype="array"))])
        self.assertEqual(scan_references(original, REGISTRY)["summary"]["unresolved"], 1)

    def test_local_scope_does_not_escape_branch_or_cross_group(self):
        branch = node("branch-declare", {"paramTypes": "custom", "value": "''", "code": "''"}, "oldSource", depth=["if"])
        original = design([branch, node("outside", reference("oldSource"))])
        self.assertEqual(scan_references(original, REGISTRY)["summary"]["unresolved"], 1)
        original["actionData"][0]["data"][1]["depth"] = ["if"]
        self.assertEqual(scan_references(original, REGISTRY)["summary"]["automatic"], 1)
        other = deepcopy(original["actionData"][0])
        other.update(key="other", data=[node("other-use", reference("oldSource"))])
        original["actionData"].append(other)
        self.assertEqual(scan_references(original, REGISTRY)["summary"]["unresolved"], 1)

    def test_duplicate_declarations_are_ambiguous(self):
        nodes = [node("first", {"paramTypes": "custom", "value": "''", "code": "''"}, "oldSource"),
                 node("second", {"paramTypes": "custom", "value": "''", "code": "''"}, "oldSource"),
                 node("use", reference("oldSource"))]
        self.assertEqual(scan_references(design(nodes), REGISTRY)["summary"]["ambiguous"], 1)

    def test_projection_changes_only_reported_bindings_and_keeps_action_target(self):
        original = design()
        original["actionData"][0]["data"].append({"key": "call", "elementKey": "CallAction", "depth": [],
            "paramsValue": {"inputParams": {"actionName": {"value": "missing-action"}}, "outputParams": {}}})
        projection = inspection_projection(original, scan_references(original, REGISTRY))
        self.assertEqual(projection["actionData"][0]["data"][1], original["actionData"][0]["data"][1])
        self.assertEqual(original["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["variableValue"]["paramTypes"], "inputParam")

    def test_unknown_type_and_property_selection_require_review(self):
        original = design([node("use", reference(dtype=""))])
        self.assertEqual(scan_references(original, REGISTRY)["summary"]["ambiguous"], 1)
        original["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["variableValue"]["objectAttribute"] = "source.foo"
        self.assertEqual(scan_references(original, REGISTRY)["summary"]["unresolved"], 1)

    def test_block_output_is_visible_inside_its_block_only(self):
        declaration = node("loop", {"paramTypes": "custom", "value": "''", "code": "''"}, "iterator")
        declaration["elementKey"] = "If"
        original = design([declaration, node("use", reference("iterator"))])
        self.assertEqual(scan_references(original, REGISTRY)["summary"]["unresolved"], 1)
        original["actionData"][0]["data"][1]["depth"] = ["loop"]
        self.assertEqual(scan_references(original, REGISTRY)["summary"]["automatic"], 1)

    def test_nested_bindings_are_batched_into_one_native_node_operation(self):
        original = design()
        values = original["actionData"][0]["data"][0]["paramsValue"]["inputParams"]
        values["rows"] = [{"value": reference()}, {"value": reference()}]
        report = scan_references(original, REGISTRY)
        repaired = build_repair(original, report, [])
        self.assertEqual(len(repaired["repairs"]), 3)
        self.assertEqual(len(repaired["operations"]), 1)
        self.assertEqual(scan_references(repaired["design"], REGISTRY)["summary"]["total"], 0)

    def test_csharp_output_binding_uses_native_csharp_expression(self):
        original = design()
        group = original["actionData"][0]
        group["actionType"] = "csharp"
        group["outputParams"] = [{"key": "new-output", "title": "result", "type": "string"}]
        group["data"][0]["paramsValue"]["inputParams"]["variableValue"] = reference("result", kind="outputParam")
        repaired = build_repair(original, scan_references(original, REGISTRY), [])
        self.assertEqual(repaired["repairs"][0]["after"]["code"], 'outputParams["result"]')

    def test_approval_summary_includes_local_provenance_and_full_before_after(self):
        declaration = node("declare", {"paramTypes": "custom", "value": "''", "code": "''"}, "oldSource")
        original = design([declaration, node("use", reference("oldSource"))])
        repaired = build_repair(original, scan_references(original, REGISTRY), [])
        text = reference_repair_summary(repaired["repairs"])
        self.assertIn("当前作用域局部变量 · declare", text)
        self.assertIn("obsolete-key", text)
        self.assertIn("localVariable-oldSource", text)

    def test_repair_compile_rejects_unresolved_or_regenerated_invalid_reference(self):
        original = design()
        bridge = CompilerBridge(editor_config())
        with patch.object(bridge, "describe_registry", return_value=REGISTRY), patch.object(bridge, "_call") as call:
            with self.assertRaisesRegex(CompilerError, "still contains"):
                bridge.compile_reference_repair({"design": original, "operations": [], "repairs": []})
            call.assert_not_called()
        repaired = build_repair(original, scan_references(original, REGISTRY), [])
        with patch.object(bridge, "describe_registry", return_value=REGISTRY), patch.object(
            bridge, "validate_design"
        ), patch.object(bridge, "compile_patch", return_value={"design": original}):
            with self.assertRaisesRegex(CompilerError, "reintroduced"):
                bridge.compile_reference_repair(repaired)

    def test_reference_repair_always_compiles_full_with_original_baseline(self):
        original = design()
        repaired = build_repair(original, scan_references(original, REGISTRY), [])
        saved = {"js_code": "saved-js", "csharp_code": "", "relation_data": {}}
        bridge = CompilerBridge(editor_config())
        with patch.object(bridge, "describe_registry", return_value=REGISTRY), patch.object(
            bridge, "validate_design"
        ), patch.object(bridge, "compile_patch", return_value={"design": repaired["design"]}) as compile_patch:
            bridge.compile_reference_repair(repaired, baseline_design=original, baseline_artifacts=saved)
        self.assertEqual(compile_patch.call_args.kwargs, {
            "baseline_design": original, "baseline_artifacts": saved, "generation_mode": "full"})


class RepairServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = object.__new__(EditorService)
        self.service.state = StateStore(self.root / "state.sqlite3")
        self.draft = baseline(data=json.dumps(design()))
        self.draft["data_json"] = design()
        self.compiler = FakeCompiler()
        self.compiler.describe_registry = lambda: {**self.compiler.compiler_binding(), **REGISTRY}
        self.compiler.validate_design = Mock(return_value={"validation": {"valid": True}})
        self.repository = Mock()
        self.repository.load_draft_by_identifier.return_value = self.draft
        self.repository.load_draft_by_id.return_value = self.draft
        self.repository.load_relations.return_value = []
        self.components = (editor_config(), object(), self.repository, self.compiler, UnavailableRuntimeValidator(editor_config().runtime_references))

    def start(self):
        with patch.object(self.service, "_components", return_value=self.components), patch.object(
            self.service, "editor_status", return_value={"planning_ready": True, "write_ready": True, "production_ready": True, "gates": {}}
        ), patch("gxp_editor.service.runtime_root", return_value=self.root), patch(
            "gxp_editor.backup.runtime_root", return_value=self.root
        ), patch("gxp_editor.service.approval_secret", return_value="test-secret"):
            return self.service.begin_reference_repair_session("action")

    def test_repair_entry_backs_up_original_before_structural_inspection(self):
        def inspect(projection):
            snapshots = list((self.root / "backups").rglob("design.json"))
            self.assertEqual(len(snapshots), 1)
            stored = json.loads(snapshots[0].read_text(encoding="utf-8"))
            self.assertEqual(stored["data"], self.draft["data"])
            self.assertNotEqual(projection["actionData"][0]["data"][0]["paramsValue"], self.draft["data_json"]["actionData"][0]["data"][0]["paramsValue"])
            return {"validation": {"valid": True}}
        self.compiler.validate_design.side_effect = inspect
        result = self.start()
        self.assertEqual(result["session_mode"], "reference_repair")
        self.assertFalse(result["original_canvas_validated"])
        self.assertEqual(result["reference_summary"]["automatic"], 1)

    def test_non_reference_error_does_not_create_session(self):
        self.compiler.validate_design.side_effect = CompilerError("invalid depth")
        with self.assertRaisesRegex(CompilerError, "invalid depth"):
            self.start()
        self.assertIsNone(self.service.state.active_session())
        self.assertTrue(list((self.root / "backups").rglob("design.json")))

    def test_failed_whole_canvas_compile_creates_no_plan_or_approval(self):
        session = self.start()
        self.compiler.compile_reference_repair = Mock(side_effect=CompilerError("whole candidate invalid"))
        with patch.object(self.service, "_components", return_value=self.components), patch(
            "gxp_editor.service.runtime_root", return_value=self.root
        ), self.assertRaisesRegex(CompilerError, "whole candidate invalid"):
            self.service.plan_reference_repairs(session["session_id"], review_spec={
                "schema_version": 1, "revision": "repair-check", "scope_policy": "manual_review", "requirements": []})
        self.assertFalse((self.root / "plans").exists())
        self.assertFalse((self.root / "transactions").exists())

    def test_repair_session_cannot_accept_arbitrary_canvas_operations(self):
        session = self.start()
        with self.assertRaisesRegex(EditorServiceError, "plan_reference_repairs"):
            self.service.plan_canvas_patch(session["session_id"], [])

    def test_draft_drift_blocks_inspection_and_plan(self):
        session = self.start()
        self.repository.load_draft_by_id.return_value = {**self.draft, "content_hash": "changed"}
        with patch.object(self.service, "_components", return_value=self.components):
            with self.assertRaisesRegex(EditorServiceError, "Draft changed"):
                self.service.inspect_reference_repairs(session["session_id"])

    def test_ambiguity_creates_no_plan_and_does_not_call_compiler(self):
        declaration = node("declare", {"paramTypes": "custom", "value": "''", "code": "''"}, "source")
        self.draft["data_json"] = design([declaration, node("use", reference())])
        self.draft["data"] = json.dumps(self.draft["data_json"])
        session = self.start()
        self.compiler.compile_reference_repair = Mock()
        with patch.object(self.service, "_components", return_value=self.components):
            result = self.service.plan_reference_repairs(session["session_id"])
        self.assertTrue(result["blocked"])
        self.assertFalse(result["plan_created"])
        self.compiler.compile_reference_repair.assert_not_called()

    def test_active_session_blocks_repair_entry_before_backup_or_compiler(self):
        session = self.start()
        with patch.object(self.service, "_components") as components, patch.object(self.service, "abort_edit_session", return_value={}):
            result = self.service.begin_reference_repair_session("another")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["active_session_id"], session["session_id"])
        components.assert_not_called()


if __name__ == "__main__":
    unittest.main()
