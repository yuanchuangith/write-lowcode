from __future__ import annotations

import asyncio
import os
import subprocess
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.helpers import editor_config
from tests.test_conversation_tools import SERVER
from gxp_editor.compiler import CompilerBridge, CompilerError
from gxp_editor.operation_preflight import preflight_batch


ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return {"code": "GroupParameterRenameTest", "globalVariables": [], "actionData": [{
        "key": "formatRows", "title": "格式化列", "actionType": "js", "data": [],
        "inputParams": [{"key": "rowInput", "title": "rowData ", "type": "array", "required": True,
                         "description": "Rows to format", "extension": {"nativeFlag": True}},
                        {"key": "otherInput", "title": "options", "type": "object", "required": False}],
        "outputParams": [{"key": "result", "title": "result", "type": "array"}],
    }, {"key": "untouched", "title": "Other action", "actionType": "js", "inputParams": [], "data": []}]}


def operation(**extra):
    return {"operation": "rename_group_input_parameter", "action_group_key": "formatRows",
            "parameter_key": "rowInput", "expected_old_name": "rowData ", "new_name": "rowData", **extra}


class GroupParameterRenameContractTests(unittest.TestCase):
    def test_node_adapter_contract(self):
        result = subprocess.run(["node", "--test", str(ROOT / "tests" / "group_parameter_rename.test.cjs")],
                                capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_mcp_preserves_the_exact_old_name_without_constructing_live_editor_services(self):
        request = operation()
        with patch.object(SERVER, "service") as factory:
            factory.return_value.plan_canvas_patch.return_value = {"write_executed": False}
            asyncio.run(SERVER.mcp.call_tool("plan_canvas_patch", {
                "conversation_id": "test-conversation", "session_id": "test-session", "operations": [request]}))
            forwarded = factory.return_value.plan_canvas_patch.call_args.args[1]
            self.assertEqual(forwarded, [request])
            self.assertEqual(forwarded[0]["expected_old_name"], "rowData ")

    def test_mcp_rejects_missing_identity_and_extra_replacement_fields_before_service_entry(self):
        missing = operation()
        del missing["parameter_key"]
        for request in (missing, operation(parameter_key=1), operation(type="string"),
                        operation(input_params=[]), operation(js_code="injected")):
            with self.subTest(request=request), patch.object(SERVER, "service") as factory:
                with self.assertRaises(Exception):
                    asyncio.run(SERVER.mcp.call_tool("plan_canvas_patch", {"session_id": "s", "operations": [request]}))
                factory.assert_not_called()

    def test_preflight_simulates_the_whitespace_fix_without_mutation_or_native_processes(self):
        design, operations = fixture(), [operation()]
        before = deepcopy((design, operations))
        with patch("subprocess.run", side_effect=AssertionError("Preflight must stay data-only")):
            result = preflight_batch(design, operations)
        self.assertEqual(result["issue_count"], 0)
        self.assertFalse(result["all_no_change"])
        self.assertFalse(result["write_executed"])
        self.assertEqual((design, operations), before)

    def test_preflight_sequential_old_name_guards_and_no_op_detection(self):
        result = preflight_batch(fixture(), [operation(), operation(expected_old_name="rowData", new_name="rows")])
        self.assertEqual(result["issue_count"], 0)
        stale = preflight_batch(fixture(), [operation(), operation(new_name="rows")])
        self.assertEqual(stale["issue_count"], 1)
        self.assertEqual(stale["issues"][0]["operation_index"], 1)
        design = fixture()
        design["actionData"][0]["inputParams"][0]["title"] = "rowData"
        self.assertTrue(preflight_batch(design, [operation(expected_old_name="rowData")])["all_no_change"])
        self.assertEqual(preflight_batch(design, [operation()])["issue_count"], 1)

    def test_preflight_rejects_bad_guards_conflicts_ambiguous_identities_and_csharp(self):
        def duplicate_parameter(design):
            design["actionData"][0]["inputParams"].append(deepcopy(design["actionData"][0]["inputParams"][0]))

        def duplicate_group(design):
            design["actionData"].append(deepcopy(design["actionData"][0]))

        def csharp(design):
            design["actionData"][0]["actionType"] = "csharp"

        cases = [(None, operation(expected_old_name="rowData")),
                 (None, operation(parameter_key="missing")), (None, operation(action_group_key="missing")),
                 (None, operation(new_name="options")), (duplicate_parameter, operation()),
                 (duplicate_group, operation()), (csharp, operation())]
        cases += [(None, operation(new_name=name)) for name in
                  ("rowData ", "row-data", "1row", "row.data", "inbiz", "Inbiz", "__proto__", "constructor", "prototype", "return")]
        for prepare, request in cases:
            design = fixture()
            if prepare:
                prepare(design)
            before = deepcopy(design)
            with self.subTest(request=request, prepare=prepare):
                result = preflight_batch(design, [request])
                self.assertEqual(result["issue_count"], 1)
                self.assertEqual(design, before)


@unittest.skipUnless(os.environ.get("GXP_TEST_COMPILER_BRIDGE"), "Set GXP_TEST_COMPILER_BRIDGE for native compiler integration")
class NativeGroupParameterRenameTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ["GXP_TEST_COMPILER_BRIDGE"]).resolve()
        self.bridge = CompilerBridge(replace(editor_config(), frontend_repo=str(path.parents[2]), compiler_bridge=str(path)))

    def generated_fixture(self):
        design = fixture()
        design["actionData"][0]["inputParams"][0]["title"] = "rowData"
        result = self.bridge.compile_patch(design, [{
            "operation": "append_node", "action_group_key": "formatRows", "element_key": "SetVariable",
            "input_params": {"variableType": "number", "variableValue": {
                "paramTypes": "custom", "value": "rowData.length", "code": "rowData.length",
                "label": "rowData.length", "dataType": "number"}},
            "output_params": {"variableName": {"paramTypes": "custom", "value": "rowCount", "code": "rowCount",
                                                 "label": "rowCount", "dataType": "number"}},
        }, {
            "operation": "append_node", "action_group_key": "formatRows", "element_key": "ExitAction",
            "input_params": {"returnValue": {"paramTypes": "inputParam", "value": "rowInput", "code": "rowData",
                                              "label": "rowData", "dataType": "array"}}, "output_params": {},
        }])
        return result["design"]

    def test_native_compiler_repairs_signature_preserves_configuration_and_executes_rows(self):
        design = self.generated_fixture()
        design["actionData"][0]["inputParams"][0]["title"] = "rowData "
        before = deepcopy(design)
        with self.assertRaises(CompilerError) as original_error:
            self.bridge.compile_patch(design, [{
                "operation": "append_node", "action_group_key": "untouched", "element_key": "ExitAction",
                "input_params": {"returnValue": {"paramTypes": "custom", "value": "true", "code": "true",
                                                  "label": "true", "dataType": "boolean"}}, "output_params": {},
            }])
        self.assertEqual(original_error.exception.details["reason_code"], "callback_signature_mismatch")
        self.assertEqual(design, before)
        result = self.bridge.compile_patch(design, [operation()])
        expected = deepcopy(design)
        expected["actionData"][0]["inputParams"][0]["title"] = "rowData"
        self.assertEqual(result["design"], expected)
        self.assertEqual(design, before)
        self.assertIn("rename_group_input_parameter", self.bridge.describe_registry()["supported_operations"])
        self.assertTrue(self.bridge.verify_compiled_result(result)["code_generation_complete"])
        self.assertRegex(result["js_code"], r"self\.formatRows\s*=\s*(?:async\s+)?function\s*\(\s*rowData\s*=\s*\[\]")
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const factory=vm.runInNewContext('('+fs.readFileSync(0,'utf8')+')');
const self=factory(()=>({}),{}),rows=[{id:'synthetic-a'},{id:'synthetic-b'}];
Promise.resolve(self.formatRows(rows,{})).then(result=>{
  assert.equal(result,rows);assert.deepEqual(rows,[{id:'synthetic-a'},{id:'synthetic-b'}]);
}).catch(error=>{console.error(error);process.exitCode=1;});
'''
        executed = subprocess.run(["node", "-e", script], input=result["js_code"],
                                  capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(executed.returncode, 0, executed.stderr)

    def test_ordinary_rename_leaves_broken_expression_to_existing_lexical_validation(self):
        design = self.generated_fixture()
        before = deepcopy(design)
        with self.assertRaises(CompilerError) as error:
            self.bridge.compile_patch(design, [operation(expected_old_name="rowData", new_name="rows")])
        self.assertEqual(error.exception.details["reason_code"], "undeclared_identifier")
        self.assertEqual(design, before)
