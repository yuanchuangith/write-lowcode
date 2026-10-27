from __future__ import annotations

import json
import subprocess
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import PropertyMock, patch

from tests.helpers import editor_config
from gxp_editor.compiler import CompilerBridge, CompilerError


def fixture():
    return {"actionData": [{"key": "g", "actionType": "js", "data": [{"key": "n", "elementKey": "Query",
            "paramsValue": {"inputParams": {"status": "active", "enabled": True, "a/b": {"~label": "old"}, "rows": [{"id": "r", "value": 1}]}, "outputParams": {}}}]}], "globalVariables": []}


def operation(**extra):
    return {"operation": "set_parameter", "action_group_key": "g", "target_node_key": "n", "expected_element_key": "Query",
            "path": "/inputParams/status", "expected_old_value": "active", "value": "active", **extra}


class CompilerOptimizationTests(unittest.TestCase):
    def test_no_change_preflight_requires_no_node_or_source_registry(self):
        design = fixture()
        with patch("gxp_editor.compiler.subprocess.run", side_effect=AssertionError("No subprocess expected")):
            self.assertTrue(CompilerBridge.guarded_no_changes(design, [operation()]))
            self.assertTrue(CompilerBridge.guarded_no_changes(design, [operation(path="/inputParams/a~1b/~0label", expected_old_value="old", value="old")]))
            self.assertTrue(CompilerBridge.guarded_no_changes(design, [operation(path="/inputParams/rows", expected_old_value=[{"value": 1, "id": "r"}], value=[{"id": "r", "value": 1}])]))
            self.assertFalse(CompilerBridge.guarded_no_changes(design, [operation(value="changed")]))
            self.assertFalse(CompilerBridge.guarded_no_changes(design, [{"operation": "replace_params"}]))
            self.assertFalse(CompilerBridge.guarded_no_changes(design, []))

    def test_no_change_preflight_rejects_stale_and_unsafe_inputs_with_details(self):
        for extra, reason in [({"expected_old_value": "stale"}, "old_value_mismatch"),
                ({"path": "/inputParams/enabled", "expected_old_value": 1, "value": 1}, "old_value_mismatch"),
                ({"path": "/inputParams/constructor"}, "invalid_parameter_path"),
                ({"path": "/inputParams/missing"}, "parameter_path_missing"),
                ({"path": "/inputParams/rows/0/value"}, "array_index_forbidden"),
                ({"expected_element_key": "Other"}, "element_type_mismatch"),
                ({"target_node_key": "missing"}, "target_not_found"),
                ({"unexpected": True}, "invalid_operation")]:
            with self.subTest(extra=extra), self.assertRaises(CompilerError) as caught:
                CompilerBridge.guarded_no_changes(fixture(), [operation(**extra)])
            self.assertEqual(caught.exception.details["reason_code"], reason)
            self.assertEqual(caught.exception.details["operation_index"], 0)

    def test_no_change_does_not_accept_partial_or_inconsistent_custom_paraminput(self):
        design = fixture()
        node = design["actionData"][0]["data"][0]
        node["elementKey"] = "SetVariable"
        record = {"paramTypes": "custom", "value": "1", "code": "1"}
        node["paramsValue"]["inputParams"]["variableValue"] = record
        with self.assertRaises(CompilerError) as caught:
            CompilerBridge.guarded_no_changes(design, [operation(expected_element_key="SetVariable", path="/inputParams/variableValue/code", expected_old_value="1", value="1")])
        self.assertEqual(caught.exception.details["reason_code"], "paraminput_record_required")
        request = operation(expected_element_key="SetVariable", path="/inputParams/variableValue", expected_old_value=deepcopy(record), value=deepcopy(record))
        self.assertTrue(CompilerBridge.guarded_no_changes(design, [request]))
        record["code"] = "2"
        request.update(expected_old_value=deepcopy(record), value=deepcopy(record))
        with self.assertRaises(CompilerError) as caught:
            CompilerBridge.guarded_no_changes(design, [request])
        self.assertEqual(caught.exception.details["reason_code"], "paraminput_unsynchronized")

    def test_no_change_to_other_field_does_not_demand_old_paraminput_repair(self):
        design = fixture(); node = design['actionData'][0]['data'][0]; node['elementKey'] = 'SetVariable'
        node['paramsValue']['inputParams']['variableValue'] = {'paramTypes':'custom','value':'display','code':'true'}
        self.assertTrue(CompilerBridge.guarded_no_changes(design, [operation(expected_element_key='SetVariable')]))

    def test_combined_verification_calls_one_process_and_still_compares_every_artifact(self):
        bridge = CompilerBridge(editor_config())
        design = fixture()
        artifacts = {"js_code": "generated", "csharp_code": "", "relation_data": {}}
        regenerated = {"command": "verify_candidate", "validation": {"valid": True}, "compiler_binding": {},
                       "event_binding_validation": {"binding_node_keys": []}, **artifacts}
        for field in (None, "js_code", "csharp_code", "relation_data"):
            candidate = deepcopy(artifacts)
            if field:
                candidate[field] = {"changed": True} if field == "relation_data" else "changed"
            with patch.object(bridge, "_call", return_value=deepcopy(regenerated)) as call, patch.object(bridge, "_bind_registry_hash", side_effect=lambda result: result):
                if field:
                    with self.assertRaisesRegex(CompilerError, "does not match"):
                        bridge._verify_candidate(design, candidate, binding_node_keys=[])
                else:
                    self.assertEqual(bridge._verify_candidate(design, candidate, binding_node_keys=[]), regenerated)
                self.assertEqual(call.call_count, 1)
                self.assertEqual(call.call_args.args[0]["command"], "verify_candidate")

    def test_node_operation_error_details_are_decoded_and_bounded(self):
        bridge = CompilerBridge(editor_config())
        details = {"operation_index": 2, "group_key": "g" * 1000, "node_key": "n", "path": "/inputParams/status", "reason_code": "old_value_mismatch", "value": "private"}
        response = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "error": "Old value changed", "details": details}), "")
        # Use an existing file for the bridge existence gate; source verification is isolated here.
        with patch.object(type(bridge), "path", new_callable=PropertyMock, return_value=Path(__file__)), patch.object(bridge, "_verify_native_sources", return_value=""), patch.object(bridge, "_node_identity", return_value=("fixture-node", "v22.0.0")), patch("gxp_editor.compiler.subprocess.run", return_value=response):
            with self.assertRaises(CompilerError) as caught:
                bridge._call({"command": "compile_patch"})
        self.assertEqual(caught.exception.details["operation_index"], 2)
        self.assertEqual(len(caught.exception.details["group_key"]), 256)
        self.assertNotIn("value", caught.exception.details)

    def test_combined_result_rejects_changed_event_evidence_or_source_binding(self):
        bridge = CompilerBridge(editor_config())
        result = {"design": fixture(), "js_code": "generated", "csharp_code": "", "relation_data": {},
                  "validation": {"valid": True}, "compiler_binding": {}, "event_binding_validation": {"binding_node_keys": []}}
        regenerated = {**result, "command": "verify_candidate", "event_binding_validation": {"binding_node_keys": ["changed"]}}
        with patch.object(bridge, "_call", return_value=regenerated), patch.object(bridge, "_bind_registry_hash", side_effect=lambda value: value):
            with self.assertRaisesRegex(CompilerError, "Event binding validation evidence changed"):
                bridge.verify_compiled_result(result)
        with patch.object(bridge, "_call", return_value=regenerated), patch.object(bridge, "_bind_registry_hash", side_effect=CompilerError("Compiler source changed")):
            with self.assertRaisesRegex(CompilerError, "Compiler source changed"):
                bridge.verify_compiled_result(result)

    def test_operation_forbidden_fields_get_location_details_before_node_start(self):
        bridge = CompilerBridge(editor_config())
        with patch.object(bridge, "_call", side_effect=AssertionError("No subprocess expected")):
            with self.assertRaises(CompilerError) as caught:
                bridge.compile_patch(fixture(), [operation(sql="private SQL")])
        details = caught.exception.details
        self.assertEqual({key: details[key] for key in ("operation_index", "group_key", "node_key", "path", "reason_code")},
            {"operation_index": 0, "group_key": "g", "node_key": "n", "path": "/inputParams/status", "reason_code": "invalid_operation"})
        self.assertEqual(details["phase"], "operation_preflight")
        self.assertTrue(details["issues"])


if __name__ == "__main__":
    unittest.main()
