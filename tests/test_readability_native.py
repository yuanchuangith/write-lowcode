"""The two revision-owner entry points retain short-circuit and awaited writes."""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path

from tests.helpers import editor_config
from gxp_editor.compiler import CompilerBridge
from gxp_editor.readability import analyze_readability


ENTRIES = ("initialize", "defaultReviser")
NOTE = "修订模式下，修订人为空且能取到复审发起人时带入；已有值保留。"
PROCESS = 'String(inbiz.queryData.processId || "").split(":")[0] === "review"'
VERSION = 'Number(String(inbiz.queryData.processId || "").split(":")[1]) >= 16'
ADD = 'inbiz.getPageMode() === "add"'
FIRST = 'relatedAttributes.getWorkFlowInfo(inbiz,"activityNo") === "firstTask"'
INBOX = 'inbiz.queryData.source === "inbox"'
ONGOING = 'inbiz("FlowStatus").value === "inprocess"'
REVISE = 'inbiz("ExecMode").value === "revised"'
HAS_REVISER = '!!inbiz("Reviser")'
BLANK_REVISER = ('(Array.isArray(inbiz("Reviser").value) ? inbiz("Reviser").value.length === 0 : '
                 '["", "[]", "null"].includes(String(inbiz("Reviser").value || "").trim()))')
HAS_USER = '!!inbiz("UserId")'
HAS_USER_VALUE = 'String(inbiz("UserId").value || "").trim() !== ""'
WRITE = ('await inbiz("Reviser").setValueToID([{memberType:"user", '
         'memberIds:[String(inbiz("UserId").value).trim()]}])')
PREDICATE = ' && '.join([PROCESS, VERSION, '(' + ADD + ' || (' + ' && '.join([FIRST, INBOX, ONGOING]) + '))',
                         REVISE, HAS_REVISER, BLANK_REVISER, HAS_USER, HAS_USER_VALUE])


def param(code):
    return {"paramTypes": "custom", "value": code, "code": code, "label": code}


def leaf(code):
    # Raw boolean leaf; the native Equal operator would otherwise emit `==`.
    return {"target": param(code)}


def filters(logic, *children):
    return {"Logic": logic, "Filters": list(children)}


@unittest.skipUnless(os.environ.get("GXP_TEST_COMPILER_BRIDGE"), "Set GXP_TEST_COMPILER_BRIDGE for native integration")
class ReviewReviserTests(unittest.TestCase):
    def test_expression_checks_distinguish_retained_logic_and_unexecuted_callbacks(self):
        root = Path(os.environ["GXP_TEST_COMPILER_BRIDGE"]).resolve().parents[2]

        def canvas(source):
            node = {"key": "value", "elementKey": "SetVariable", "paramsValue": {
                "inputParams": {"variableValue": param(source)},
                "outputParams": {"variableName": param("target")},
            }}
            return {"actionData": [{"key": "main", "actionType": "js", "data": [node]}]}

        empty = {"actionData": []}
        for code in ('"inbiz(\\\"SomeField\\\").setValue(1)"', '() => inbiz("SomeField").setValue(1)',
                     '((inbiz) => inbiz("SomeField").setValue(1))(adapter)'):
            with self.subTest(code=code):
                self.assertFalse(analyze_readability(empty, canvas(code), frontend_root=root))
        stored = canvas('(() => inbiz("SomeField").setValue(1))()')
        self.assertEqual("hidden_platform_write", analyze_readability(empty, stored, frontend_root=root)[0]["rule"])
        self.assertFalse(analyze_readability(stored, stored, frontend_root=root))

    def test_both_native_candidates_preserve_review_reviser_behavior(self):
        path = Path(os.environ["GXP_TEST_COMPILER_BRIDGE"]).resolve()
        bridge = CompilerBridge(replace(editor_config(), frontend_repo=str(path.parents[2]), compiler_bridge=str(path)))
        empty = {"actionData": [{"key": key, "title": key, "actionType": "js", "data": []} for key in ENTRIES],
                 "globalVariables": [], "code": "ReviewReviserFixture"}
        operations = []
        for key in ENTRIES:
            operations.extend([
                {"operation": "append_node", "action_group_key": key, "element_key": "SetVariable",
                 "input_params": {"variableType": "boolean", "variableValue": param(PREDICATE)},
                 "output_params": {"variableName": param("shouldDefaultReviewReviser")}},
                {"operation": "append_node", "action_group_key": key, "element_key": "IfCondition",
                 "input_params": {"condition": leaf("shouldDefaultReviewReviser")}, "node_alias": key + "Guard"},
                {"operation": "insert_in_block", "action_group_key": key, "target_node_key": "@" + key + "Guard",
                 "element_key": "SetVariable", "input_params": {"variableType": "any", "variableValue": param(WRITE)},
                 "output_params": {"variableName": param("writeResult")}},
            ])
        before = bridge.compile_patch(empty, operations)
        rows = analyze_readability(empty, before["design"], frontend_root=path.parents[2])
        self.assertEqual(2, sum(row["rule"] == "hidden_platform_write" for row in rows))
        self.assertEqual(2, sum(row["rule"] == "wrapped_single_use_predicate" for row in rows))
        condition = filters("And", leaf(PROCESS), leaf(VERSION),
            filters("Or", leaf(ADD), filters("And", leaf(FIRST), leaf(INBOX), leaf(ONGOING))),
            *map(leaf, [REVISE, HAS_REVISER, BLANK_REVISER, HAS_USER, HAS_USER_VALUE]))
        operations = []
        for group in before["design"]["actionData"]:
            key = group["key"]
            variable, anchor = group["data"][:2]
            operations.extend([
                {"operation": "insert_before", "action_group_key": key, "anchor_node_key": anchor["key"],
                 "element_key": "CustomExt", "input_params": {"content": NOTE}},
                {"operation": "replace_params", "action_group_key": key, "target_node_key": anchor["key"],
                 "input_params": {"condition": condition}, "output_params": {}},
                {"operation": "delete_block", "action_group_key": key, "target_node_key": variable["key"]},
            ])
        after = bridge.compile_patch(before["design"], operations)
        for group in after["design"]["actionData"]:
            self.assertEqual(["CustomExt", "IfCondition", "SetVariable", "IfEnd"],
                             [node["elementKey"] for node in group["data"]])
            self.assertEqual(NOTE, group["data"][0]["paramsValue"]["inputParams"]["content"])
        self.assertFalse([row for row in analyze_readability(before["design"], after["design"], frontend_root=path.parents[2])
                          if row["status"] == "failed"])
        script = Path(__file__).with_name("review_reviser_fixture.cjs")
        payload = {"before": before["js_code"], "after": after["js_code"], "entries": ENTRIES}
        run = subprocess.run(["node", str(script)], input=json.dumps(payload), capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
