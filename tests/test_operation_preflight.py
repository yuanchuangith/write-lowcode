from __future__ import annotations

import copy
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from tests.helpers import baseline, editor_config  # Add mcp to the test path.
from gxp_editor.operation_preflight import diagnostic_context_ref, enrich_diagnostics, preflight_batch
from gxp_editor.compiler import CompilerBridge, CompilerError


def fixture():
    return {"actionData": [{"key": "g", "title": "Action", "actionType": "js", "data": [
        {"key": "n", "elementKey": "Query", "paramsValue": {"inputParams": {
            "status": "active", "rows": [{"id": "r1", "value": 1}], "nested": {"a/b": {"~label": "old"}}}, "outputParams": {}}}]}]}


def operation(**extra):
    return {"operation": "set_parameter", "action_group_key": "g", "target_node_key": "n", "expected_element_key": "Query",
            "path": "/inputParams/status", "expected_old_value": "active", "value": "changed", **extra}


class OperationPreflightTests(unittest.TestCase):
    def test_checks_later_operation_after_changed_first_without_subprocess_or_mutation(self):
        design, ops = fixture(), [operation(), operation(path="/inputParams/missing")]
        before = copy.deepcopy((design, ops))
        with patch("subprocess.run", side_effect=AssertionError("No process allowed")):
            result = preflight_batch(design, ops)
        self.assertEqual(result["issue_count"], 1)
        self.assertEqual(result["issues"][0]["operation_index"], 1)
        self.assertEqual(result["issues"][0]["reason_code"], "parameter_path_missing")
        self.assertEqual((design, ops), before)
        self.assertFalse(result["write_executed"])

    def test_parameter_updates_follow_sequential_guard_values(self):
        self.assertEqual(preflight_batch(fixture(), [operation(), operation(expected_old_value="changed", value="last")])["issues"], [])
        result = preflight_batch(fixture(), [operation(), operation()])
        self.assertEqual(result["issues"][0]["reason_code"], "old_value_mismatch")
        self.assertTrue(preflight_batch(fixture(), [operation(value="active")])["all_no_change"])
        self.assertFalse(preflight_batch(fixture(), [operation()])["all_no_change"])

    def test_failed_operation_does_not_hide_independent_errors_in_same_group(self):
        design = fixture()
        design["actionData"][0]["data"].append({**copy.deepcopy(design["actionData"][0]["data"][0]), "key": "other"})
        result = preflight_batch(design, [operation(expected_old_value="wrong"),
            operation(path="/inputParams/missing"),
            operation(target_node_key="other", expected_old_value="also wrong")])
        self.assertEqual([(row["operation_index"], row["reason_code"]) for row in result["issues"]],
            [(0, "old_value_mismatch"), (1, "parameter_path_missing"), (2, "old_value_mismatch")])
        self.assertEqual(result["pending_checks"], [])

    def test_failed_paraminput_update_rolls_back_before_independent_operation(self):
        design = fixture()
        node = design["actionData"][0]["data"][0]
        node["elementKey"] = "SetVariable"
        record = {"paramTypes": "custom", "value": "1", "code": "1"}
        node["paramsValue"]["inputParams"]["variableValue"] = record
        op = operation(expected_element_key="SetVariable", path="/inputParams/variableValue",
            expected_old_value=record, value={**record, "value": "2"})
        result = preflight_batch(design, [op, operation(expected_element_key="SetVariable")])
        self.assertEqual([(row["operation_index"], row["reason_code"]) for row in result["issues"]],
                         [(0, "paraminput_unsynchronized")])
        self.assertEqual(result["pending_checks"], [])
        self.assertEqual(node["paramsValue"]["inputParams"]["variableValue"], record)

    def test_failed_value_dependencies_are_pending_but_unrelated_guards_are_errors(self):
        result = preflight_batch(fixture(), [operation(expected_old_value="wrong"),
            operation(expected_old_value="changed", value="last"),
            operation(expected_old_value="last", value="final"), operation(expected_old_value="unrelated")])
        self.assertEqual([(row["operation_index"], row["reason_code"]) for row in result["issues"]],
                         [(0, "old_value_mismatch"), (3, "old_value_mismatch")])
        self.assertEqual([(row["operation_index"], row["reason_code"]) for row in result["pending_checks"]],
                         [(1, "prior_failed_operation_required"), (2, "prior_failed_operation_required")])

    def test_failed_alias_creator_defers_only_its_dependents(self):
        create = {"operation": "create_action_group", "group_alias": "handler", "group_title": "Handler", "action_type": "invalid"}
        append = {"operation": "append_node", "action_group_key": "@handler", "element_key": "If", "node_alias": "block"}
        child = {"operation": "insert_in_block", "action_group_key": "@handler", "target_node_key": "@block", "element_key": "Query"}
        result = preflight_batch(fixture(), [create, append, child, operation(path="/inputParams/missing")])
        self.assertEqual([row["operation_index"] for row in result["issues"]], [0, 3])
        self.assertEqual([row["operation_index"] for row in result["pending_checks"]], [1, 2])
        failed_insert = {"operation": "append_node", "action_group_key": "g", "element_key": "If", "node_alias": "block", "unsupported": True}
        result = preflight_batch(fixture(), [failed_insert, {**child, "action_group_key": "g"}, operation(path="/inputParams/missing")])
        self.assertEqual([row["operation_index"] for row in result["issues"]], [0, 2])
        self.assertEqual([row["operation_index"] for row in result["pending_checks"]], [1])

    def test_escaped_paths_arrays_and_exact_boolean_types(self):
        self.assertEqual(preflight_batch(fixture(), [operation(path="/inputParams/nested/a~1b/~0label", expected_old_value="old")])["issues"], [])
        array = operation(operation="set_array_item", path="/inputParams/rows", item_key={"field": "id", "value": "r1"},
                          expected_old_value={"id": "r1", "value": 1}, value={"id": "r1", "value": 2})
        self.assertEqual(preflight_batch(fixture(), [array])["issues"], [])
        for extra, reason in [({"item_key": {"field": "id", "value": True}}, "invalid_item_key"),
                              ({"value": {"id": "r2", "value": 2}}, "item_identity_changed"),
                              ({"expected_old_value": {"id": "r1", "value": True}}, "old_value_mismatch"),
                              ({"path": "/inputParams/rows/0"}, "array_index_forbidden")]:
            with self.subTest(extra=extra):
                self.assertEqual(preflight_batch(fixture(), [{**array, **extra}])["issues"][0]["reason_code"], reason)

    def test_whole_paraminput_required_and_custom_value_code_synchronized(self):
        design = fixture()
        node = design["actionData"][0]["data"][0]
        node["elementKey"] = "SetVariable"
        record = {"paramTypes": "custom", "value": "1", "code": "1"}
        node["paramsValue"]["inputParams"]["variableValue"] = record
        op = operation(expected_element_key="SetVariable", path="/inputParams/variableValue", expected_old_value=record, value={**record, "value": "2", "code": "2"})
        self.assertEqual(preflight_batch(design, [op])["issues"], [])
        self.assertEqual(preflight_batch(design, [{**op, "value": {**record, "value": "2"}}])["issues"][0]["reason_code"], "paraminput_unsynchronized")
        self.assertEqual(preflight_batch(design, [{**op, "path": "/inputParams/variableValue/code", "expected_old_value": "1", "value": "2"}])["issues"][0]["reason_code"], "paraminput_record_required")

    def test_alias_dependencies_are_ordered_and_native_outcomes_pending(self):
        create = {"operation": "create_action_group", "group_alias": "handler", "group_title": "Handler", "action_type": "js"}
        append = {"operation": "append_node", "action_group_key": "@handler", "element_key": "If", "node_alias": "block"}
        child = {"operation": "insert_in_block", "action_group_key": "@handler", "target_node_key": "@block", "element_key": "Query"}
        result = preflight_batch(fixture(), [create, append, child])
        self.assertEqual(result["issues"], [])
        self.assertEqual(len(result["pending_checks"]), 3)
        self.assertFalse(result["complete"])
        self.assertEqual(preflight_batch(fixture(), [create, child, append])["issues"][0]["reason_code"], "unknown_node_alias")
        other = {**child, "action_group_key": "g"}
        self.assertEqual(preflight_batch(fixture(), [create, append, other])["issues"][0]["reason_code"], "node_alias_group_mismatch")

    def test_native_structure_does_not_lead_to_guessed_later_targets(self):
        native = {"operation": "replace_block", "action_group_key": "g", "target_node_key": "n", "element_key": "If"}
        result = preflight_batch(fixture(), [native, operation(target_node_key="generated-later")])
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["pending_checks"][1]["reason_code"], "prior_native_result_required")
        # Independent malformed operation shape is still rejected after pending native work.
        self.assertEqual(preflight_batch(fixture(), [native, operation(unexpected=True)])["issues"][0]["reason_code"], "invalid_operation")

    def test_many_errors_are_bounded_and_never_echo_parameter_values(self):
        secret = "private data must never appear"
        result = preflight_batch(fixture(), [operation(sql=secret, expected_old_value=secret) for _ in range(100)])
        self.assertEqual(result["issue_count"], 100)
        self.assertEqual(len(result["issues"]), 20)
        self.assertTrue(result["issues_truncated"])
        self.assertNotIn(secret, str(result))

    def test_bad_batch_or_operation_types_are_data_errors(self):
        for ops in (None, [], [None], [operation(operation={})], [operation(value=float("nan"))], [operation(value={"__proto__": True})]):
            with self.subTest(ops=ops):
                self.assertGreater(preflight_batch(fixture(), ops)["issue_count"], 0)

    def test_compiler_error_preserves_rich_preflight_without_values_or_node_start(self):
        secret = "private old value or code"
        with patch("subprocess.run", side_effect=AssertionError("No process allowed")):
            with self.assertRaises(CompilerError) as caught:
                CompilerBridge.preflight_operations(fixture(), [operation(), operation(path="/inputParams/missing", expected_old_value=secret)])
        details = caught.exception.details
        self.assertEqual(details["phase"], "operation_preflight")
        self.assertEqual(details["operation_index"], 1)
        self.assertEqual(details["issues"][0]["reason_code"], "parameter_path_missing")
        self.assertEqual(details["expected_shape"]["operation"], "set_parameter")
        self.assertIn("expected_old_value", details["expected_shape"]["required_fields"])
        self.assertEqual(details["refresh_scope"], "submitted_operations")
        self.assertNotIn(secret, str(details))

    def test_compiler_error_keeps_lexical_and_native_drift_location_only(self):
        details = {"phase": "javascript_lexical", "reason_code": "undeclared_identifier", "group_key": "g",
                   "mapping_status": "group_only", "identifier": "missing", "line": 2, "column": 8,
                   "issues": [{"group_key": "g", "reason_code": "undeclared_identifier", "identifier": "missing", "mapping_status": "group_only", "line": 2, "column": 8}],
                   "issue_count": 1, "diff_paths": ["/paramsValue/inputParams/status"], "diff_paths_truncated": False,
                   "value": "private data", "code": "private source"}
        bounded = CompilerError("Rejected", details=details).details
        self.assertEqual(bounded["mapping_status"], "group_only")
        self.assertEqual(bounded["issues"][0]["identifier"], "missing")
        self.assertEqual(bounded["line"], 2)
        self.assertEqual(bounded["diff_paths"], details["diff_paths"])
        self.assertFalse(bounded["diff_paths_truncated"])
        self.assertNotIn("value", bounded)
        self.assertNotIn("code", bounded)

    def test_error_context_ref_reads_only_proven_baseline_target(self):
        result = preflight_batch(fixture(), [operation(expected_old_value="stale")])
        ref = result["issues"][0]["context_ref"]
        self.assertEqual(ref["tool"], "inspect_edit_context")
        self.assertEqual(ref["arguments"]["targets"], [{"group_key": "g", "node_key": "n"}])
        self.assertEqual(ref["arguments_from_failed_request"], ["session_id", "conversation_id"])
        self.assertNotIn("stale", str(ref))
        design = {**fixture(), "id": "draft-id", "refId": "action-ref"}
        missing = diagnostic_context_ref(design, {"group_key": "g", "node_key": "missing"})
        self.assertEqual(missing["scope"], "group_only")
        self.assertEqual(missing["tool"], "inspect_action")
        self.assertEqual(missing["arguments"]["version"], "draft")
        self.assertEqual(missing["arguments"]["group"], "g")
        self.assertNotIn("node_key", missing["arguments"])
        self.assertEqual(missing["arguments"]["identifier"], "action-ref")
        unresolved = diagnostic_context_ref(design, {"group_key": "@generated", "node_key": "@new"})
        self.assertFalse(unresolved["available"])
        self.assertNotIn("tool", unresolved)

    def test_group_only_lexical_diagnostics_never_invent_node_mapping(self):
        design = {**fixture(), "id": "draft-id"}
        ref = diagnostic_context_ref(design, {"group_key": "g", "node_key": "n", "mapping_status": "group_only"})
        self.assertEqual(ref["scope"], "group_only")
        self.assertEqual(ref["provider"], "gxp-lowcode-readonly")
        self.assertNotIn("node_key", ref["arguments"])
        missing_payload = enrich_diagnostics(None, {"group_key": "g", "mapping_status": "group_only", "reason_code": "undeclared_identifier"})
        self.assertEqual(missing_payload["context_ref"]["scope"], "group_only")
        self.assertFalse(missing_payload["context_ref"]["available"])

    def test_truncated_errors_have_explicit_repair_and_resubmit_continuation(self):
        report = preflight_batch(fixture(), [operation(sql="private") for _ in range(100)])
        bounded = CompilerError("Rejected", details={**report, **report["issues"][0]}).details
        self.assertTrue(bounded["issues_truncated"])
        self.assertEqual(bounded["issue_count"], 100)
        self.assertEqual(len(bounded["issues"]), 20)
        self.assertIn("Only the first 20", bounded["next_step"])
        self.assertIn("complete batch", bounded["next_step"])
        self.assertIn("Remaining issues are returned", bounded["next_step"])
        self.assertTrue(all("context_ref" in issue for issue in bounded["issues"]))

    def test_node_error_enrichment_handles_verify_payload_and_absent_design(self):
        bridge = CompilerBridge(editor_config())
        details = {"phase": "javascript_lexical", "reason_code": "undeclared_identifier", "group_key": "g",
                   "mapping_status": "group_only", "identifier": "missing", "issues": [{"group_key": "g",
                   "mapping_status": "group_only", "reason_code": "undeclared_identifier", "identifier": "missing"}], "issue_count": 1}
        for status in (0, 1):
            for design in (None, {**fixture(), "id": "draft-id"}):
                with self.subTest(status=status, design_present=design is not None):
                    response = subprocess.CompletedProcess([], status, json.dumps({"ok": False, "error": "Rejected", "details": details}), "")
                    payload = {"command": "verify_candidate", **({"design": design} if design is not None else {})}
                    with patch.object(type(bridge), "path", new_callable=PropertyMock, return_value=Path(__file__)), \
                         patch.object(bridge, "_verify_native_sources", return_value=""), \
                         patch.object(bridge, "_node_identity", return_value=("fixture-node", "v22.0.0")), \
                         patch("gxp_editor.compiler.subprocess.run", return_value=response), self.assertRaises(CompilerError) as caught:
                        bridge._call(payload)
                    ref = caught.exception.details["issues"][0]["context_ref"]
                    self.assertEqual(ref["scope"], "group_only")
                    if design is None:
                        self.assertFalse(ref["available"])
                    else:
                        self.assertEqual(ref["tool"], "inspect_action")
                        self.assertNotIn("node_key", ref["arguments"])


if __name__ == "__main__":
    unittest.main()
