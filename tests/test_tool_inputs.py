"""Exercise actual MCP validation without constructing editor/database services."""
import asyncio
import unittest
from unittest.mock import patch

from tests.test_conversation_tools import SERVER


class ToolInputTests(unittest.TestCase):
    def call(self, name, arguments):
        return asyncio.run(SERVER.mcp.call_tool(name, arguments))

    def test_guarded_and_explicit_null_values_reach_service_unchanged(self):
        base = {"operation": "set_parameter", "action_group_key": "g", "target_node_key": "n",
                "expected_element_key": "DataFilter", "path": "/inputParams/filter", "value": None}
        for guard in ({"guard_ref": "signed-token"}, {"expected_old_value": None}):
            operation = {**base, **guard}
            with self.subTest(guard=guard), patch.object(SERVER, "service") as factory:
                factory.return_value.plan_canvas_patch.return_value = {"write_executed": False}
                self.call("plan_canvas_patch", {"session_id": "s", "operations": [operation]})
                args = factory.return_value.plan_canvas_patch.call_args.args
                self.assertEqual([operation], args[1])

    def test_invalid_operations_never_construct_a_service(self):
        base = {"operation": "set_parameter", "action_group_key": "g", "target_node_key": "n",
                "expected_element_key": "DataFilter", "path": "/inputParams/filter", "value": None}
        invalid = [base, {**base, "guard_ref": "token", "expected_old_value": None},
                   {**base, "guard_ref": "token", "js_code": "injected"},
                   {**base, "guard_ref": "token", "operation": "execute_sql"},
                   {**base, "guard_ref": "token", "target_node_key": 123}]
        for operation in invalid:
            with self.subTest(operation=operation), patch.object(SERVER, "service") as factory:
                with self.assertRaises(Exception):
                    self.call("plan_canvas_patch", {"session_id": "s", "operations": [operation]})
                factory.assert_not_called()

    def test_review_spec_preserves_missing_and_null_expectations(self):
        review = {"schema_version": 1, "revision": "r1", "requirements": [{
            "id": "r", "text": "Keep the result empty", "source_text": "Keep the result empty",
            "targets": [{"group_key": "main"}]}], "scenarios": {"schema_version": 1, "cases": [{
                "id": "empty", "steps": [{"kind": "call_group", "group_key": "main"}],
                "expect": {"last_return": None}}]}}
        with patch.object(SERVER, "service") as factory:
            factory.return_value.plan_canvas_patch.return_value = {"write_executed": False}
            self.call("plan_canvas_patch", {"session_id": "s", "operations": [], "review_spec": review})
            self.assertEqual(review, factory.return_value.plan_canvas_patch.call_args.kwargs["review_spec"])
        for broken in ({**review, "scope_policy": "ignore"},
                       {**review, "scenarios": {"schema_version": 1, "cases": [{"id": "bad", "steps": []}]}}):
            with patch.object(SERVER, "service") as factory:
                with self.assertRaises(Exception):
                    self.call("plan_canvas_patch", {"session_id": "s", "operations": [], "review_spec": broken})
                factory.assert_not_called()

    def test_exact_types_and_legacy_empty_options_are_not_coerced(self):
        review = {"schema_version": 1, "revision": "r", "requirements": [],
                  "scope_policy": "manual_review", "scenarios": {"schema_version": 1, "cases": []}}
        invalid = [{**review, "schema_version": value} for value in (True, 1.0)]
        invalid += [{**review, "scenarios": {"schema_version": True, "cases": []}}]
        invalid += [{**review, "scenarios": {"schema_version": 1, "cases": [{"id": "x",
            "steps": [{"kind": "read_metadata"}], "expect": {"last_return_undefined": 1}}]}}]
        for spec in invalid:
            with self.subTest(spec=spec), patch.object(SERVER, "service") as factory:
                with self.assertRaises(Exception):
                    self.call("plan_canvas_patch", {"session_id": "s", "operations": [], "review_spec": spec})
                factory.assert_not_called()
        operations = [{"operation": "create_action_group", "group_alias": "a", "group_title": "A",
                       "action_type": "js", "event_contract": None},
                      {"operation": "bind_component_event", "action_group_key": "a", "component_id": "c",
                       "component_type": "Button", "event_name": "click", "target_action_group_key": "b",
                       "target_node_key": None, "position": ""},
                      {"operation": "set_array_item", "action_group_key": "g", "target_node_key": "n",
                       "expected_element_key": "DataFilter", "path": "/inputParams/rows",
                       "item_key": {"field": "id", "value": 1.0}, "expected_old_value": {}, "value": {}}]
        with patch.object(SERVER, "service") as factory:
            factory.return_value.plan_canvas_patch.return_value = {"write_executed": False}
            self.call("plan_canvas_patch", {"session_id": "s", "operations": operations})
            forwarded = factory.return_value.plan_canvas_patch.call_args.args[1]
            self.assertEqual(operations, forwarded)
            self.assertIs(type(forwarded[2]["item_key"]["value"]), float)

    def test_limits_and_enums_reject_before_service_construction(self):
        for name, arguments in [
            ("find_edit_nodes", {"session_id": "s", "limit": 101}),
            ("find_edit_nodes", {"session_id": "s", "offset": -1}),
            ("inspect_edit_context", {"session_id": "s", "targets": [{"group_key": "g"}]}),
            ("read_plan_review", {"plan_id": "p", "section": "sql"}),
            ("read_plan_review", {"plan_id": "p", "max_output_bytes": 65537}),
            ("prepare_write", {"plan_id": "p", "response_detail": "all"}),
        ]:
            with self.subTest(name=name, arguments=arguments), patch.object(SERVER, "service") as factory:
                with self.assertRaises(Exception):
                    self.call(name, arguments)
                factory.assert_not_called()

    def test_public_schema_describes_operations_and_review_shapes(self):
        tool = next(t for t in asyncio.run(SERVER.mcp.list_tools()) if t.name == "plan_canvas_patch")
        schema = tool.inputSchema
        operation = schema["properties"]["operations"]["items"]
        self.assertGreater(len(operation["anyOf"]), 2)
        definitions = schema["$defs"]
        guarded = definitions["SetParameterGuarded"]
        self.assertIn("guard_ref", guarded["required"])
        self.assertNotIn("expected_old_value", guarded["properties"])
        self.assertFalse(guarded["additionalProperties"])
        self.assertIn("requirements", definitions["ReviewSpec"]["required"])


if __name__ == "__main__":
    unittest.main()
