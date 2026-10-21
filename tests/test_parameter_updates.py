from __future__ import annotations

import os
import subprocess
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.helpers import editor_config
from gxp_editor.compiler import CompilerBridge, CompilerError


ROOT = Path(__file__).resolve().parents[1]


class ParameterUpdateContractTests(unittest.TestCase):
    def test_guarded_parameter_adapter_contract(self):
        result = subprocess.run(
            ["node", "--test", str(ROOT / "tests" / "parameter_updates.test.cjs")],
            capture_output=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(os.environ.get("GXP_TEST_COMPILER_BRIDGE"), "Set GXP_TEST_COMPILER_BRIDGE for native compiler integration")
class NativeParameterUpdateTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ["GXP_TEST_COMPILER_BRIDGE"]).resolve()
        self.bridge = CompilerBridge(replace(editor_config(), frontend_repo=str(path.parents[2]), compiler_bridge=str(path)))

    def variable_design(self, action_type="js"):
        initial = {"actionData": [{"key": "main", "title": "Initialization", "actionType": action_type, "data": []}],
                   "globalVariables": [], "code": "ParameterUpdateTest"}
        return self.bridge.compile_patch(initial, [{
            "operation": "append_node", "action_group_key": "main", "element_key": "SetVariable",
            "input_params": {"variableType": "number", "variableValue": {"paramTypes": "custom", "value": "1", "code": "1", "label": "value", "dataType": "number"}},
            "output_params": {"variableName": {"paramTypes": "custom", "value": "answer", "code": "answer", "label": "answer", "dataType": "number"}},
        }, {
            "operation": "append_node", "action_group_key": "main", "element_key": "OutputLog",
            "input_params": {"params": [{"name": {"paramTypes": "localVariable", "value": "localVariable-answer", "code": "answer", "label": "answer", "dataType": "number"}}]},
            "output_params": {},
        }])["design"]

    def test_native_regeneration_preserves_other_parameters_and_executes_new_value(self):
        design = self.variable_design()
        node = design["actionData"][0]["data"][0]
        old = deepcopy(node["paramsValue"]["inputParams"]["variableValue"])
        operations = [{"operation": "set_parameter", "action_group_key": "main", "target_node_key": node["key"],
                       "expected_element_key": "SetVariable", "path": "/inputParams/variableValue",
                       "expected_old_value": old, "value": {**old, "value": "2", "code": "2"}}]
        before = deepcopy((design, operations))
        result = self.bridge.compile_patch(design, operations)
        generated = result["design"]["actionData"][0]["data"][0]
        expected = deepcopy(node["paramsValue"])
        expected["inputParams"]["variableValue"] = {**old, "value": "2", "code": "2"}
        self.assertEqual(generated["paramsValue"], expected)
        self.assertEqual(generated["key"], node["key"])
        self.assertEqual((design, operations), before)
        self.assertTrue(self.bridge.verify_compiled_result(result)["code_generation_complete"])
        self.assertIn("set_parameter", self.bridge.describe_registry()["supported_operations"])
        # Independent execution proves the updated expression is used by generated JS.
        script = "const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');const source=fs.readFileSync(0,'utf8'),logs=[];const action=vm.runInNewContext('('+source+')',{console:{log:(...args)=>logs.push(args)}})(()=>({}),{});Promise.resolve(action.main()).then(()=>{assert.equal(logs.length,1);assert.equal(logs[0][1],2);}).catch(e=>{console.error(e);process.exitCode=1;});"
        executed = subprocess.run(["node", "-e", script], input=result["js_code"], capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(executed.returncode, 0, executed.stderr)

    def test_native_csharp_parameter_update_is_independently_regenerated(self):
        design = self.variable_design("csharp")
        node = design["actionData"][0]["data"][0]
        old = deepcopy(node["paramsValue"]["inputParams"]["variableValue"])
        result = self.bridge.compile_patch(design, [{"operation": "set_parameter", "action_group_key": "main", "target_node_key": node["key"],
                                                   "expected_element_key": "SetVariable", "path": "/inputParams/variableValue",
                                                   "expected_old_value": old, "value": {**old, "value": "2", "code": "2"}}])
        self.assertIn("Convert.ToInt32(2)", result["csharp_code"])
        self.assertEqual(result["design"]["actionData"][0]["data"][0]["paramsValue"]["outputParams"], node["paramsValue"]["outputParams"])
        self.assertTrue(self.bridge.verify_compiled_result(result)["code_generation_complete"])

    def test_native_normalization_cannot_silently_change_another_parameter(self):
        design = self.variable_design()
        node = design["actionData"][0]["data"][0]
        with self.assertRaisesRegex(CompilerError, "outside the requested value"):
            self.bridge.compile_patch(design, [{"operation": "set_parameter", "action_group_key": "main", "target_node_key": node["key"],
                                               "expected_element_key": "SetVariable", "path": "/inputParams/variableType",
                                               "expected_old_value": "number", "value": "string"}])

    def test_native_stable_row_edit_and_single_process_verification(self):
        def row(identity, value):
            return {"id": identity, "key": identity, "value": {"paramTypes": "custom", "value": value, "code": value, "label": value, "dataType": "string"}}
        rows = [row("second", '"two"'), row("first", '"one"')]
        initial = {"actionData": [{"key": "main", "title": "Initialization", "actionType": "js", "data": []}], "globalVariables": []}
        design = self.bridge.compile_patch(initial, [{"operation": "append_node", "action_group_key": "main", "element_key": "AddMetaData",
            "input_params": {"config": {"paramSettings": rows}}, "output_params": {}}])["design"]
        node = design["actionData"][0]["data"][0]
        replacement = row("first", '"updated"')
        request = {"operation": "set_array_item", "action_group_key": "main", "target_node_key": node["key"], "expected_element_key": "AddMetaData",
                   "path": "/inputParams/config/paramSettings", "item_key": {"field": "id", "value": "first"}, "expected_old_value": rows[1], "value": replacement}
        result = self.bridge.compile_patch(design, [request])
        self.assertEqual(result["design"]["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["config"]["paramSettings"], [rows[0], replacement])
        with patch.object(self.bridge, "_call", wraps=self.bridge._call) as call:
            self.assertTrue(self.bridge.verify_compiled_result(result)["code_generation_complete"])
            self.assertEqual(call.call_count, 1)
            self.assertEqual(call.call_args.args[0]["command"], "verify_candidate")
        no_change = self.bridge.compile_patch(result["design"], [{**request, "expected_old_value": replacement}])
        self.assertTrue(no_change["no_change"])
        self.assertEqual(no_change["design"], result["design"])
        with self.assertRaises(CompilerError) as caught:
            self.bridge.compile_patch(result["design"], [request])
        self.assertEqual(caught.exception.details, {"operation_index": 0, "group_key": "main", "node_key": node["key"],
                         "path": "/inputParams/config/paramSettings", "reason_code": "old_value_mismatch"})

    def test_native_array_parameter_replacement_preserves_existing_rows(self):
        row = {"name": {"paramTypes": "custom", "value": "first", "code": "first", "label": "message"}}
        design = self.bridge.compile_patch({"actionData": [{"key": "main", "title": "Initialization", "actionType": "js", "data": []}], "globalVariables": []}, [
            {"operation": "append_node", "action_group_key": "main", "element_key": "OutputLog", "input_params": {"params": [row]}, "output_params": {}}
        ])["design"]
        node = design["actionData"][0]["data"][0]
        added = {"name": {"paramTypes": "custom", "value": "second", "code": "second", "label": "message"}}
        result = self.bridge.compile_patch(design, [{"operation": "set_parameter", "action_group_key": "main", "target_node_key": node["key"],
                                                   "expected_element_key": "OutputLog", "path": "/inputParams/params",
                                                   "expected_old_value": [row], "value": [row, added]}])
        self.assertEqual(result["design"]["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["params"], [row, added])
        self.assertIn("first", result["js_code"])
        self.assertIn("second", result["js_code"])
        self.assertTrue(self.bridge.verify_compiled_result(result)["code_generation_complete"])


if __name__ == "__main__":
    unittest.main()
