from __future__ import annotations

import json
import os
import subprocess
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.helpers import editor_config
from gxp_editor.compiler import CompilerBridge, CompilerError


class CodegenConsistencyUnitTests(unittest.TestCase):
    def test_uuid_override_restores_after_compiler_failure(self):
        runner = Path(__file__).resolve().parents[1] / "mcp" / "gxp_editor" / "compiler_runner.cjs"
        script = r'''
const assert = require('node:assert/strict');
const runner = require(process.argv[1]);
const fallback = () => Buffer.from([7, 7, 7, 7]);
assert.deepEqual(runner.uuidBytes(4, fallback), fallback());
let first;
assert.throws(() => runner.reproducibleCompile({actionData: []}, () => {
  first = runner.uuidBytes(4, fallback);
  throw new Error('native compilation failed');
}), /native compilation failed/);
assert.deepEqual(runner.uuidBytes(4, fallback), fallback());
const repeated = runner.reproducibleCompile({actionData: []}, () => runner.uuidBytes(4, fallback));
assert.deepEqual(first, repeated);
'''
        run = subprocess.run(["node", "-e", script, str(runner)], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_canvas_regeneration_rejects_each_mismatched_artifact(self):
        bridge = CompilerBridge(editor_config())
        design = {"actionData": [], "globalVariables": []}
        expected = {"js_code": "function () { return 1; }", "csharp_code": "// compiled", "relation_data": {"node": {"value": 1}}}
        regenerated = {"ok": True, "command": "compile_design", "validation": {"valid": True}, **expected}
        for field, replacement in (("js_code", "function () { return 2; }"), ("csharp_code", "// different"), ("relation_data", {})):
            with self.subTest(field=field):
                candidate = {**expected, field: replacement}
                with patch.object(bridge, "_call", return_value=regenerated) as call, patch.object(bridge, "_bind_registry_hash", side_effect=lambda result: result):
                    with self.assertRaisesRegex(CompilerError, field + ".*candidate canvas"):
                        bridge._verify_codegen_consistency(design, candidate)
                    self.assertEqual(call.call_args.args[0], {"command": "compile_design", "design": design})


@unittest.skipUnless(os.environ.get("GXP_TEST_COMPILER_BRIDGE"), "Native compiler path required")
class NativeCodegenConsistencyTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ["GXP_TEST_COMPILER_BRIDGE"]).resolve()
        self.bridge = CompilerBridge(replace(editor_config(), frontend_repo=str(path.parents[2]), compiler_bridge=str(path)))
        self.empty = {"actionData": [{"key": "main", "title": "初始化", "actionType": "js", "data": []}], "globalVariables": []}

    @staticmethod
    def variable(name, expression, data_type="string", group="main"):
        def custom(code):
            return {"paramTypes": "custom", "value": code, "code": code, "label": code, "dataType": data_type}
        return {"operation": "append_node", "action_group_key": group, "element_key": "SetVariable",
                "input_params": {"variableType": data_type, "variableValue": custom(expression)},
                "output_params": {"variableName": custom(name)}}

    def test_temporary_uuids_are_reproducible_without_rewriting_business_source(self):
        callback = json.loads((Path(__file__).parent / "fixtures" / "manual_child_button_callback.json").read_text(encoding="utf-8"))
        design = {"actionData": [callback], "globalVariables": []}
        first = self.bridge._call({"command": "compile_design", "design": design})
        # JSON object order does not change temporary identifiers.
        reordered = {"globalVariables": [], "actionData": [callback]}
        second = self.bridge._call({"command": "compile_design", "design": reordered})
        self.assertEqual(first["js_code"], second["js_code"])
        self.assertEqual(first["csharp_code"], second["csharp_code"])
        self.assertEqual(first["relation_data"], second["relation_data"])
        self.assertIn("metaDataObj_", first["js_code"])
        result = self.bridge.compile_patch(self.empty, [self.variable("literal", '"metaDataObj_00000000"'), self.variable("draw", "Math.random()", "number")])
        self.assertIn('var literal = "metaDataObj_00000000";', result["js_code"])
        self.assertIn("var draw = Math.random();", result["js_code"])
        self.assertTrue(self.bridge.verify_compiled_result(result)["generated_code_consistency_verified"])

    def test_csharp_random_temporary_variables_are_reproducible(self):
        design = {"code": "CodegenConsistency", "actionData": [
            {"key": "backend", "title": "后端入口", "actionType": "csharp", "data": []},
            {"key": "target", "title": "后端子动作", "actionType": "csharp", "inputParams": [],
             "outputParams": [{"key": "value", "title": "value", "type": "string"}], "data": []}], "globalVariables": []}
        call = {"operation": "append_node", "action_group_key": "backend", "element_key": "CallAction",
                "input_params": {"actionName": {"value": "target", "label": "后端子动作", "actionType": "csharp"}},
                "output_params": {"received": {"paramTypes": "custom", "value": "received", "code": "received",
                    "label": "received", "paramName": "value", "dataType": "string"}}}
        result = self.bridge.compile_patch(design, [call])
        regenerated = self.bridge._call({"command": "compile_design", "design": result["design"]})
        self.assertEqual(result["csharp_code"], regenerated["csharp_code"])
        self.assertRegex(result["csharp_code"], r"var target_[A-Z0-9]+_Output = await this.target")

    def test_canvas_node_keys_remain_native_random(self):
        operation = [self.variable("result", '"expected"')]
        first = self.bridge.compile_patch(self.empty, operation)
        second = self.bridge.compile_patch(self.empty, operation)
        self.assertNotEqual(first["design"]["actionData"][0]["data"][0]["key"], second["design"]["actionData"][0]["data"][0]["key"])

    def test_approval_rejects_valid_javascript_that_does_not_match_canvas(self):
        result = self.bridge.compile_patch(self.empty, [self.variable("result", '"expected"')])
        desired = {key: deepcopy(result[key]) for key in ("js_code", "csharp_code", "relation_data")}
        desired["js_code"] = desired["js_code"].replace('"expected"', '"different"')
        with self.assertRaisesRegex(CompilerError, "js_code.*candidate canvas"):
            self.bridge.verify_candidate_artifact(result["design"], desired, baseline_design=self.empty)

    def test_approval_rejects_csharp_or_relations_that_do_not_match_canvas(self):
        design = {"code": "CodegenConsistency", "actionData": [{"key": "backend", "title": "后端动作", "actionType": "csharp", "data": []}], "globalVariables": []}
        result = self.bridge.compile_patch(design, [self.variable("result", '"expected"', group="backend")])
        original = {key: deepcopy(result[key]) for key in ("js_code", "csharp_code", "relation_data")}
        self.assertTrue(self.bridge.verify_candidate_artifact(result["design"], original, baseline_design=design)["generated_code_consistency_verified"])
        for field, changed in (("csharp_code", original["csharp_code"].replace('"expected"', '"different"')), ("relation_data", {"invented": {"value": 1}})):
            with self.subTest(field=field):
                desired = {**original, field: changed}
                with self.assertRaisesRegex(CompilerError, field + ".*candidate canvas"):
                    self.bridge.verify_candidate_artifact(result["design"], desired, baseline_design=design)


if __name__ == "__main__":
    unittest.main()
