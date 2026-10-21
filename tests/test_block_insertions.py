"""Regression for a new IF containing two filters in one immutable-baseline plan."""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from tests.helpers import editor_config
from gxp_editor.compiler import CompilerBridge, CompilerError


ROOT = Path(__file__).resolve().parents[1]


class BlockInsertionContractTests(unittest.TestCase):
    def test_canvas_adapter_contracts(self):
        result = subprocess.run(["node", "--test", str(ROOT / "tests/canvas_operations.test.cjs")],
                                capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(os.environ.get("GXP_TEST_COMPILER_BRIDGE"), "Set GXP_TEST_COMPILER_BRIDGE for native integration")
class NativeBlockInsertionTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ["GXP_TEST_COMPILER_BRIDGE"]).resolve()
        self.bridge = CompilerBridge(replace(editor_config(), frontend_repo=str(path.parents[2]), compiler_bridge=str(path)))
        self.design = {"actionData": [{"key": "main", "title": "Initialization", "actionType": "js", "data": [],
                                       "inputParams": [{"key": "materialType", "title": "materialType", "type": "string"}]}],
                       "globalVariables": [], "code": "BlockInsertionTest"}

    @staticmethod
    def condition(expression='materialType === "skill"'):
        return {"condition": {"target": {"paramTypes": "custom", "value": expression, "code": expression, "label": expression}}}

    @staticmethod
    def filter_params(component):
        return {"name": {"value": component, "label": component}, "whereType": "staticWhere",
                "whereConditions": {"Logic": "And", "Filters": [{"Field": "type", "Operator": "Equal",
                    "ParamInput": {"paramTypes": "custom", "value": '"offline"', "code": '"offline"'}, "value": "offline"}]}}

    def test_two_filters_are_generated_inside_new_if_and_only_register_for_skill(self):
        operations = [{"operation": "append_node", "action_group_key": "main", "element_key": "IfCondition",
                       "input_params": self.condition(), "node_alias": "skillOnly"}]
        operations += [{"operation": "insert_in_block", "action_group_key": "main", "target_node_key": "@skillOnly",
                        "element_key": "DataFilter", "input_params": self.filter_params(component)}
                       for component in ("trainingMethod", "evaluationMethod")]
        original = deepcopy((self.design, operations))
        result = self.bridge.compile_patch(self.design, operations)
        nodes = result["design"]["actionData"][0]["data"]
        self.assertEqual([n["elementKey"] for n in nodes], ["IfCondition", "DataFilter", "DataFilter", "IfEnd"])
        self.assertEqual([n["depth"] for n in nodes], [[], [nodes[0]["key"]], [nodes[0]["key"]], [nodes[0]["key"]]])
        self.assertNotIn("node_alias", json.dumps(result["design"]))
        self.assertEqual((self.design, operations), original)
        self.assertTrue(self.bridge.verify_compiled_result(result)["code_generation_complete"])
        self.assertIn("insert_in_block", self.bridge.describe_registry()["supported_operations"])
        script = """
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const factory=vm.runInNewContext('('+fs.readFileSync(0,'utf8')+')');
(async()=>{
  for(const kind of ['skill','skillFile']){
    const components={trainingMethod:{},evaluationMethod:{}};
    const action=factory(id=>components[id],{whereFormat:(filter,values)=>({filter,values})});
    await action.main(kind);
    for(const component of Object.values(components)){
      assert.equal(typeof component.dynamicFilter,kind==='skill'?'function':'undefined');
      if(kind==='skill')assert.equal(component.dynamicFilter().values[0],'offline');
    }
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        executed = subprocess.run(["node", "-e", script], input=result["js_code"], capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(executed.returncode, 0, executed.stderr)

    def test_nested_block_end_is_resolved_and_insert_aliases_use_native_keys(self):
        result = self.bridge.compile_patch(self.design, [
            {"operation": "append_node", "action_group_key": "main", "element_key": "OutputLog", "input_params": {"params": []}},
        ])
        anchor = result["design"]["actionData"][0]["data"][0]["key"]
        result = self.bridge.compile_patch(result["design"], [
            {"operation": "insert_before", "action_group_key": "main", "anchor_node_key": anchor,
             "element_key": "IfCondition", "input_params": self.condition(), "node_alias": "outer"},
            {"operation": "insert_in_block", "action_group_key": "main", "target_node_key": "@outer",
             "element_key": "IfCondition", "input_params": self.condition("true"), "node_alias": "inner"},
            {"operation": "insert_in_block", "action_group_key": "main", "target_node_key": "@inner",
             "element_key": "OutputLog", "input_params": {"params": []}},
            {"operation": "insert_in_block", "action_group_key": "main", "target_node_key": "@outer",
             "element_key": "OutputLog", "input_params": {"params": []}, "node_alias": "last"},
            {"operation": "insert_after", "action_group_key": "main", "anchor_node_key": "@last",
             "element_key": "OutputLog", "input_params": {"params": []}},
        ])
        nodes = result["design"]["actionData"][0]["data"]
        self.assertEqual([n["elementKey"] for n in nodes], ["IfCondition", "IfCondition", "OutputLog", "IfEnd", "OutputLog", "OutputLog", "IfEnd", "OutputLog"])
        self.assertEqual(nodes[2]["depth"], [nodes[0]["key"], nodes[1]["key"]])
        self.assertEqual(nodes[4]["depth"], [nodes[0]["key"]])
        self.assertTrue(self.bridge.verify_compiled_result(result)["code_generation_complete"])
        with self.assertRaises(CompilerError):
            self.bridge.compile_patch(result["design"], [{"operation": "insert_in_block", "action_group_key": "main",
                                                       "target_node_key": nodes[-1]["key"], "element_key": "OutputLog"}])

    def test_if_with_else_keeps_inserted_filter_in_the_if_branch(self):
        result = self.bridge.compile_patch(self.design, [
            {"operation": "append_node", "action_group_key": "main", "element_key": "IfCondition",
             "input_params": self.condition(), "node_alias": "skillOnly"},
            {"operation": "insert_in_block", "action_group_key": "main", "target_node_key": "@skillOnly", "element_key": "Else"},
            {"operation": "insert_in_block", "action_group_key": "main", "target_node_key": "@skillOnly",
             "element_key": "DataFilter", "input_params": self.filter_params("trainingMethod")},
        ])
        nodes = result["design"]["actionData"][0]["data"]
        self.assertEqual([node["elementKey"] for node in nodes], ["IfCondition", "DataFilter", "Else", "IfEnd"])
        self.assertEqual(nodes[1]["depth"], [nodes[0]["key"]])
        self.assertTrue(self.bridge.verify_compiled_result(result)["code_generation_complete"])


if __name__ == "__main__":
    unittest.main()
