"""Typed local plan revisions retain the complete native operation contract."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import unittest
from unittest.mock import patch

from pydantic import TypeAdapter, ValidationError

from tests.test_conversation_tools import SERVER
from gxp_editor.tool_inputs import OperationRevision, ReviewSpecUpdate


def parameter_operation(**extra):
    return {"operation": "set_parameter", "action_group_key": "group", "target_node_key": "node",
            "expected_element_key": "SetVariable", "path": "/inputParams/variableValue", "expected_old_value": None,
            "value": None, **extra}


def revision(operation=None, **extra):
    return {"index": 0, "expected_operation_sha256": "a" * 64,
            "operation": operation if operation is not None else parameter_operation(), **extra}


class OperationRevisionInputTests(unittest.TestCase):
    def setUp(self):
        self.adapter = TypeAdapter(OperationRevision)

    def test_whole_native_operation_and_explicit_null_values_are_preserved(self):
        value = revision()
        original = deepcopy(value)
        result = self.adapter.validate_python(value)
        self.assertEqual(result, original)
        self.assertIn("expected_old_value", result["operation"])
        self.assertIsNone(result["operation"]["expected_old_value"])
        self.assertIsNone(result["operation"]["value"])
        self.assertEqual(value, original)

    def test_guarded_replacement_uses_existing_exactly_one_guard_variant(self):
        operation = parameter_operation()
        del operation["expected_old_value"]
        operation["guard_ref"] = "signed-baseline-token"
        result = self.adapter.validate_python(revision(operation))
        self.assertEqual(result["operation"], operation)
        with self.assertRaises(ValidationError):
            self.adapter.validate_python(revision({**operation, "expected_old_value": None}))

    def test_revision_index_is_strict_nonnegative_integer(self):
        for index in (True, False, -1, 0.0, 1.5, "0", None):
            with self.subTest(index=index), self.assertRaises(ValidationError):
                self.adapter.validate_python(revision(index=index))
        self.assertEqual(self.adapter.validate_python(revision(index=123))["index"], 123)

    def test_revision_requires_expected_hash_and_forbids_extra_outer_fields(self):
        for fingerprint in ("", "  ", None, True, 123):
            with self.subTest(fingerprint=fingerprint), self.assertRaises(ValidationError):
                self.adapter.validate_python(revision(expected_operation_sha256=fingerprint))
        for extra in ({"sql": "invalid"}, {"review_spec": {}}, {"action_group_key": "other"}):
            with self.subTest(extra=list(extra)), self.assertRaises(ValidationError):
                self.adapter.validate_python(revision(**extra))
        for missing in ("index", "expected_operation_sha256", "operation"):
            value = revision()
            del value[missing]
            with self.subTest(missing=missing), self.assertRaises(ValidationError):
                self.adapter.validate_python(value)

    def test_native_operation_validation_and_limited_branch_errors_are_retained(self):
        for operation in (
            {"operation": "execute_sql", "sql": "invalid"},
            parameter_operation(js_code="injected"),
            parameter_operation(target_node_key=123),
            {"operation": "append_node", "element_key": "OutputLog"},
        ):
            with self.subTest(operation=operation["operation"]), self.assertRaises(ValidationError) as caught:
                self.adapter.validate_python(revision(operation))
            self.assertLess(caught.exception.error_count(), 5)

    def test_schema_exposes_existing_operations_without_new_mutation_semantics(self):
        schema = self.adapter.json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), {"index", "expected_operation_sha256", "operation"})
        self.assertEqual(schema["properties"]["index"]["minimum"], 0)
        self.assertGreater(len(schema["properties"]["operation"]["anyOf"]), 2)
        self.assertIn("SetParameterGuarded", schema["$defs"])


class ReviewSpecUpdateInputTests(unittest.TestCase):
    def setUp(self):
        self.adapter = TypeAdapter(ReviewSpecUpdate)

    @staticmethod
    def requirement():
        return {"id": "rule-1", "text": "Keep the original business requirement", "source_text": "The user's unchanged wording",
                "targets": [{"group_key": "group", "node_key": "node", "field_path": "paramsValue/inputParams/value"}]}

    @staticmethod
    def assertion():
        return {"id": "assertion-1", "requirement_ids": ["rule-1"],
                "target": {"group_key": "group", "node_key": "node", "element_key": "SetVariable"},
                "path": "/inputParams/variableValue", "operator": "equals", "value": None}

    def test_revision_only_preserves_omission_and_does_not_invent_user_evidence(self):
        result = self.adapter.validate_python({"revision": "request-2"})
        self.assertEqual(result, {"revision": "request-2"})
        self.assertNotIn("requirements", result)
        self.assertNotIn("canvas_assertions", result)
        self.assertNotIn("schema_version", result)
        self.assertNotIn("change_source_text", result)

    def test_upsert_records_preserve_complete_rules_targets_and_exact_assertion_values(self):
        value = {"schema_version": 1, "revision": "request-2", "requirements": [self.requirement()],
                 "canvas_assertions": [self.assertion()], "change_source_text": "The user's new clarification"}
        original = deepcopy(value)
        result = self.adapter.validate_python(value)
        self.assertEqual(result, original)
        self.assertIn("value", result["canvas_assertions"][0])
        self.assertIsNone(result["canvas_assertions"][0]["value"])
        self.assertEqual(value, original)

    def test_partial_rule_and_assertion_records_are_rejected(self):
        for missing in ("id", "text", "source_text"):
            requirement = self.requirement()
            del requirement[missing]
            with self.subTest(missing=missing), self.assertRaises(ValidationError):
                self.adapter.validate_python({"revision": "r2", "requirements": [requirement]})
        for missing in ("id", "requirement_ids", "target", "path", "operator", "value"):
            assertion = self.assertion()
            del assertion[missing]
            with self.subTest(missing=missing), self.assertRaises(ValidationError):
                self.adapter.validate_python({"revision": "r2", "canvas_assertions": [assertion]})

    def test_unchanged_assertion_retains_existing_no_value_contract(self):
        assertion = self.assertion()
        assertion["operator"] = "unchanged"
        del assertion["value"]
        update = {"revision": "r2", "canvas_assertions": [assertion]}
        self.assertEqual(self.adapter.validate_python(update), update)
        assertion["value"] = None
        with self.assertRaises(ValidationError):
            self.adapter.validate_python({"revision": "r2", "canvas_assertions": [assertion]})

    def test_strict_schema_version_nonblank_revision_and_evidence(self):
        for value in ({}, {"revision": ""}, {"revision": " "}, {"revision": 2},
                      {"revision": "r2", "schema_version": True}, {"revision": "r2", "schema_version": 1.0},
                      {"revision": "r2", "schema_version": 2}, {"revision": "r2", "change_source_text": ""}):
            with self.subTest(fields=list(value)), self.assertRaises(ValidationError):
                self.adapter.validate_python(value)

    def test_no_delete_scope_switch_or_scenario_update_semantics_are_exposed(self):
        for extra in ({"delete_requirements": ["rule-1"]}, {"delete_assertions": ["assertion-1"]},
                      {"scope_policy": "manual_review"}, {"scenarios": {"schema_version": 1, "cases": []}},
                      {"operations": []}, {"sql": "invalid"}):
            with self.subTest(extra=list(extra)), self.assertRaises(ValidationError):
                self.adapter.validate_python({"revision": "r2", **extra})
        self.assertEqual(self.adapter.validate_python({"revision": "r2", "requirements": [], "canvas_assertions": []}),
                         {"revision": "r2", "requirements": [], "canvas_assertions": []})

    def test_public_update_schema_only_requires_revision_and_forbids_extra_fields(self):
        schema = self.adapter.json_schema()
        self.assertEqual(schema["required"], ["revision"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), {"schema_version", "revision", "requirements", "canvas_assertions", "change_source_text"})
        self.assertIn("Requirement", schema["$defs"])
        self.assertIn("ValueAssertion", schema["$defs"])


class PlanRevisionMcpInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not any(tool.name == "revise_canvas_plan" for tool in asyncio.run(SERVER.mcp.list_tools())):
            raise unittest.SkipTest("revise_canvas_plan transport is being integrated by the service owner")

    def call(self, arguments):
        return asyncio.run(SERVER.mcp.call_tool("revise_canvas_plan", arguments))

    def test_omission_reuses_saved_operations_and_inherits_review_checkpoint(self):
        with patch.object(SERVER, "service") as factory:
            factory.return_value.revise_canvas_plan.return_value = {"plan_id": "revised", "write_executed": False}
            self.call({"plan_id": "plan", "conversation_id": "owner"})
            factory.assert_called_once_with("owner")
            call = factory.return_value.revise_canvas_plan.call_args
            self.assertEqual(call.args[0], "plan")
            self.assertIsNone(call.kwargs.get("replacements"))
            self.assertIsNone(call.kwargs.get("review_spec"))
            self.assertEqual(call.kwargs.get("response_detail", "summary"), "summary")

    def test_complete_replacement_and_null_guard_reach_service_unchanged(self):
        replacements = [revision(index=4)]
        with patch.object(SERVER, "service") as factory:
            factory.return_value.revise_canvas_plan.return_value = {"write_executed": False}
            self.call({"plan_id": "plan", "conversation_id": "owner", "replacements": replacements})
            self.assertEqual(factory.return_value.revise_canvas_plan.call_args.kwargs["replacements"], replacements)

    def test_invalid_revision_shape_never_constructs_a_business_service(self):
        for value in (revision(index=True), revision(index=-1), revision(expected_operation_sha256=""),
                      revision(parameter_operation(js_code="injected")), revision(sql="invalid")):
            with self.subTest(index=value.get("index")), patch.object(SERVER, "service") as factory:
                with self.assertRaises(Exception):
                    self.call({"plan_id": "plan", "conversation_id": "owner", "replacements": [value]})
                factory.assert_not_called()

    def test_tool_schema_documents_index_hash_and_native_operation(self):
        tool = next(tool for tool in asyncio.run(SERVER.mcp.list_tools()) if tool.name == "revise_canvas_plan")
        definition = tool.inputSchema["$defs"]["OperationRevision"]
        self.assertEqual(set(definition["required"]), {"index", "expected_operation_sha256", "operation"})
        self.assertFalse(definition["additionalProperties"])

    def test_partial_review_update_preserves_omitted_scenarios_through_mcp(self):
        update = {"revision": "request-2", "requirements": [{"id": "r", "text": "Keep prior value",
                  "source_text": "User clarification: keep prior value", "targets": [{"group_key": "main"}]}]}
        with patch.object(SERVER, "service") as factory:
            factory.return_value.revise_canvas_plan.return_value = {"write_executed": False}
            self.call({"plan_id": "plan", "review_spec_update": update})
            arguments = factory.return_value.revise_canvas_plan.call_args.kwargs
            self.assertEqual(arguments["review_spec_update"], update)
            self.assertNotIn("scenarios", arguments["review_spec_update"])
            self.assertIsNone(arguments.get("review_spec"))

    def test_operation_hashes_are_readable_through_actual_mcp_schema(self):
        with patch.object(SERVER, "service") as factory:
            factory.return_value.read_plan_review.return_value = {"section": "operations", "items": []}
            asyncio.run(SERVER.mcp.call_tool("read_plan_review", {"plan_id": "plan", "section": "operations"}))
            self.assertEqual(factory.return_value.read_plan_review.call_args.kwargs["section"], "operations")


if __name__ == "__main__":
    unittest.main()
