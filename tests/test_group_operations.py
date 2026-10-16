from __future__ import annotations

import json
import os
import subprocess
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from tests.helpers import baseline, editor_config, runtime_validation, transaction
from gxp_editor.approval_display import canvas_change_summary
from gxp_editor.compiler import CompilerBridge
from gxp_editor.component_preview import component_changes, parameter_rows
from gxp_editor.hashing import sha256_json, sha256_text
from gxp_editor.transaction import build_transaction_artifact, validate_transaction_artifact


class GroupPreviewTests(unittest.TestCase):
    def test_empty_group_creation_and_parameter_change_are_reviewable(self):
        before = {"actionData": [], "globalVariables": []}
        after = {"actionData": [{"key": "new-group", "title": "点击回调", "actionType": "js",
                                 "inputParams": [{"key": "event", "title": "event", "type": "object"}], "data": []}],
                 "globalVariables": []}
        original = deepcopy((before, after))
        changes = component_changes(before, after)
        self.assertEqual(len(changes), 1)
        self.assertIsNone(changes[0]["after"]["line"])
        self.assertEqual(changes[0]["after"]["node"]["elementKey"], "actionGroup")
        self.assertTrue(any(row["key"] == "输入参数" and "event" in row["after"] for row in parameter_rows(changes[0], {})))
        self.assertIn("新增动作分组", canvas_change_summary(before, after))
        modified = deepcopy(after)
        modified["actionData"][0]["inputParams"][0]["type"] = "string"
        self.assertEqual(component_changes(after, modified)[0]["kind"], "修改")
        self.assertEqual((before, after), original)

    def test_qt_group_header_opens_empty_group_parameters(self):
        from PySide6.QtWidgets import QApplication
        from gxp_editor.qt_review_ui import ComponentReview
        app = QApplication.instance() or QApplication([])
        before = {"actionData": [], "globalVariables": []}
        after = {"actionData": [{"key": "g", "title": "空回调", "actionType": "js", "data": [],
                                 "inputParams": [{"key": "event", "title": "event", "type": "object"}]}],
                 "globalVariables": []}
        view = ComponentReview()
        self.addCleanup(view.close)
        view.render(before, after)
        app.processEvents()
        self.assertEqual(view.trees["after"].topLevelItem(0).text(1), "新增分组")
        self.assertEqual(view.selected_change["after"]["node"]["elementKey"], "actionGroup")
        self.assertGreater(view.parameters.topLevelItemCount(), 0)
        self.assertTrue(view.details_button.isEnabled())

    def test_empty_group_removal_is_visible(self):
        before = {"actionData": [{"key": "g", "title": "空分组", "actionType": "js", "data": []}]}
        changes = component_changes(before, {"actionData": []})
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["kind"], "删除")


@unittest.skipUnless(os.environ.get("GXP_TEST_COMPILER_BRIDGE"), "Set GXP_TEST_COMPILER_BRIDGE for native compiler integration")
class NativeGroupIntegrationTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ["GXP_TEST_COMPILER_BRIDGE"]).resolve()
        self.bridge = CompilerBridge(replace(editor_config(), frontend_repo=str(path.parents[2]), compiler_bridge=str(path)))

    def test_native_round_trip_and_event_execution(self):
        design = {"actionData": [{"key": "main", "title": "初始化", "actionType": "js", "data": []}], "globalVariables": []}
        contract = self.bridge.describe_component_contract("EformButton")
        self.assertFalse(contract["component_instance_verified"])
        self.assertIn("click", [event["event_name"] for event in contract["component"]["events"]])
        result = self.bridge.compile_patch(design, [
            {"operation": "create_action_group", "group_alias": "handler", "group_title": "按钮点击", "action_type": "js",
             "event_contract": {"component_type": "EformButton", "event_name": "click"}},
            {"operation": "append_node", "action_group_key": "@handler", "element_key": "OutputLog",
             "input_params": {"params": [{"name": {"paramTypes": "custom", "value": "clicked", "code": "clicked", "label": "message"}}]}, "output_params": {}},
            {"operation": "bind_component_event", "action_group_key": "main", "component_id": "submitButton",
             "component_type": "EformButton", "event_name": "click", "target_action_group_key": "@handler"},
        ])
        evidence = self.bridge.verify_compiled_result(result)
        self.assertTrue(evidence["code_generation_complete"])
        desired = {"data": json.dumps(result["design"], ensure_ascii=False),
                   "js_code": result["js_code"], "csharp_code": result["csharp_code"]}
        hashes = {"data_sha256": sha256_text(desired["data"]), "js_sha256": sha256_text(desired["js_code"]),
                  "csharp_sha256": sha256_text(desired["csharp_code"]), "relation_sha256": sha256_json(result["relation_data"])}
        approval = build_transaction_artifact(
            transaction_id="native-test", session_id="session-test", plan_id="plan-test", kind="write",
            baseline=baseline(), desired_design=desired, current_relations=[], desired_relations=result["relation_data"],
            database_safety=transaction()["database_safety"], runtime_validation=runtime_validation(desired["csharp_code"]),
            compiler_evidence={**evidence, "hashes": hashes})
        validate_transaction_artifact(approval)
        callback = result["design"]["actionData"][1]
        self.assertEqual(callback["inputParams"][0]["title"], "event")
        self.assertEqual(result["design"]["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["action"]["value"], callback["key"])
        # Execute against an isolated stub to prove the generated event invokes the new callback.
        script = r'''
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const source=fs.readFileSync(0,'utf8'); let handler;const logs=[];
const factory=vm.runInNewContext('('+source+')',{console:{log:(...args)=>logs.push(args)}});
const self=factory(id=>{assert.equal(id,'submitButton');return {configEvent:(event,fn)=>{assert.equal(event,'click');handler=fn;},on:(event,fn)=>assert.equal(fn,handler)};},{});
(async()=>{await self.main();assert.equal(typeof handler,'function');await handler({});assert.equal(logs[0][1],'clicked');})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        ran = subprocess.run(["node", "-e", script], input=result["js_code"], encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertRegex(result["compiler_binding"]["adapter_sha256"], r"^[0-9a-f]{64}$")

    def test_empty_csharp_group_generates_code(self):
        result = self.bridge.compile_patch({"actionData": [], "globalVariables": [], "code": "TestAction"}, [
            {"operation": "create_action_group", "group_alias": "server", "group_title": "后端检查", "action_type": "csharp"}])
        self.assertTrue(result["csharp_code"].strip())
        self.assertTrue(self.bridge.verify_compiled_result(result)["code_generation_complete"])
