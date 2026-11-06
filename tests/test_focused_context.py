"""Synthetic bounded context coverage; no business database or native process."""
from __future__ import annotations

from copy import deepcopy
import json
import unittest

from tests.helpers import baseline  # Add mcp to the test import path.
from tests.payload_fixtures import PayloadHarness
from tests import test_edit_context as context_fixtures
from gxp_editor.edit_context import (
    EditContextError, SUMMARY_INLINE_TOTAL_BYTES, SUMMARY_VARIABLE_TOTAL_BYTES, build_edit_context,
)
from gxp_editor.edit_guards import expand_guard_operations
from gxp_editor.hashing import canonical_json
from gxp_editor.value_transport import value_summary


class FocusedContextTests(unittest.TestCase):
    def setUp(self):
        fixture = context_fixtures.EditContextTests()
        fixture.setUp()
        self.design, self.registry, self.targets = fixture.design, fixture.registry, fixture.targets

    def summary(self, paths=None, **kwargs):
        return build_edit_context(self.design, self.registry, self.targets, response_detail="summary",
                                  parameter_paths=paths or ["/inputParams/x"], **kwargs)

    def target_inputs(self):
        return self.design["actionData"][0]["data"][3]["paramsValue"]["inputParams"]

    def test_small_strings_arrays_and_native_records_are_exact_not_previews(self):
        values = {"text": "中文🙂", "rows": [{"id": "row-1", "enabled": False}],
                  "record": {"paramTypes": "custom", "value": "source.code", "code": "source.code", "label": "source.code"}}
        self.target_inputs().update(deepcopy(values))
        paths = ["/inputParams/" + key for key in values]
        result = self.summary(paths)
        parameters = {row["path"]: row for row in result["nodes"][0]["parameters"] if "item_key" not in row}
        for key, value in values.items():
            with self.subTest(key=key):
                parameter = parameters["/inputParams/" + key]
                self.assertTrue(parameter["value_complete"])
                self.assertEqual(parameter["value"], value)
        parameters["/inputParams/record"]["value"]["value"] = "mutated response"
        self.assertEqual(self.target_inputs()["record"], values["record"])
        self.assertNotIn("params_value", result["nodes"][0])
        self.assertNotIn("native_contract", result["nodes"][0])

    def test_large_value_keeps_hash_and_focused_exact_read_hint_without_partial_value(self):
        self.target_inputs()["record"] = {"value": "中🙂" * 1000}
        parameter = self.summary(["/inputParams/record"])["nodes"][0]["parameters"][0]
        self.assertFalse(parameter["value_complete"])
        self.assertNotIn("value", parameter)
        self.assertGreater(parameter["utf8_bytes"], 1024)
        self.assertEqual(parameter["read_value"]["arguments"]["path"], "/inputParams/record")
        self.assertEqual(parameter["read_value"]["tool"], "read_edit_value")

    def test_inline_value_budget_is_shared_across_all_fields_and_targets(self):
        target = self.design["actionData"][0]["data"][3]
        target["paramsValue"]["inputParams"] = {"v" + str(index): "a" * 900 for index in range(6)}
        second = deepcopy(target)
        second["key"] = "second-target"
        self.design["actionData"][0]["data"].insert(4, second)
        self.targets.append({"group_key": "main", "node_key": "second-target"})
        parameters = [row for node in self.summary(["/inputParams/v" + str(index) for index in range(6)])["nodes"] for row in node["parameters"]]
        total = sum(row["utf8_bytes"] for row in parameters if row["value_complete"])
        self.assertLessEqual(total, SUMMARY_INLINE_TOTAL_BYTES)
        self.assertTrue(any(row["value_complete"] for row in parameters))
        self.assertTrue(any(not row["value_complete"] for row in parameters))

    def test_visible_variables_are_opt_in_and_use_exact_existing_native_scope(self):
        original = deepcopy((self.design, self.registry))
        self.assertNotIn("visible_variables", self.summary()["nodes"][0])
        context = self.summary(include_visible_variables=True)
        node = context["nodes"][0]
        self.assertEqual({row["name"] for row in node["visible_variables"]}, {"request", "result", "globalValue", "rootValue", "branchValue"})
        self.assertTrue(node["visible_variables_page"]["complete"])
        self.assertNotIn("visible_variables", context["omitted_fields"])
        full = build_edit_context(self.design, self.registry, self.targets)
        self.assertEqual(node["visible_variables"], full["nodes"][0]["visible_variables"])
        self.assertEqual((self.design, self.registry), original)
        self.design["actionData"][0]["data"][3]["depth"] = []
        names = {row["name"] for row in self.summary(include_visible_variables=True)["nodes"][0]["visible_variables"]}
        self.assertNotIn("branchValue", names)
        self.assertNotIn("futureValue", names)

    def test_variable_paging_never_truncates_a_reference_and_makes_progress(self):
        self.design["globalVariables"] = [{"name": "global" + str(index), "dataType": "string"} for index in range(40)]
        expected = build_edit_context(self.design, self.registry, self.targets)["nodes"][0]["visible_variables"]
        collected, offset = [], 0
        for _ in range(50):
            node = self.summary(include_visible_variables=True, variable_offset=offset)["nodes"][0]
            rows, page = node["visible_variables"], node["visible_variables_page"]
            self.assertTrue(page["entries_complete"])
            self.assertLessEqual(len(rows), 10)
            self.assertLessEqual(sum(len(canonical_json(row).encode("utf-8")) for row in rows), SUMMARY_VARIABLE_TOTAL_BYTES)
            collected.extend(rows)
            if page["complete"]:
                self.assertIsNone(page["next_offset"])
                break
            self.assertTrue(page["truncated"])
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual(collected, expected)

    def test_oversized_variable_is_explicit_and_uses_full_context_fallback(self):
        self.design["actionData"][0]["inputParams"][0]["title"] = "veryLong" * 1000
        node = self.summary(include_visible_variables=True)["nodes"][0]
        page = node["visible_variables_page"]
        self.assertEqual(node["visible_variables"], [])
        self.assertFalse(page["complete"])
        self.assertTrue(page["truncated"])
        self.assertGreater(page["oversized_entry"]["utf8_bytes"], SUMMARY_VARIABLE_TOTAL_BYTES)
        self.assertIn("full_context", page["fallback"])
        self.assertNotIn("reference", page["oversized_entry"])

    def test_multiple_targets_share_one_variable_byte_budget(self):
        self.design["globalVariables"] = [{"name": "global" + str(index), "dataType": "string"} for index in range(40)]
        self.targets.append({"group_key": "main", "node_key": "future"})
        self.design["actionData"][0]["data"][4]["paramsValue"]["inputParams"]["x"] = None
        nodes = self.summary(include_visible_variables=True)["nodes"]
        returned = [variable for node in nodes for variable in node["visible_variables"]]
        self.assertLessEqual(sum(len(canonical_json(row).encode("utf-8")) for row in returned), SUMMARY_VARIABLE_TOTAL_BYTES)
        self.assertTrue(all(node["visible_variables_page"]["entries_complete"] for node in nodes))
        self.assertFalse(nodes[1]["visible_variables_page"]["complete"])

    def test_new_arguments_are_strict_and_legacy_full_is_unchanged(self):
        for kwargs in ({"include_visible_variables": 1}, {"variable_offset": True}, {"variable_offset": -1}, {"variable_offset": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(EditContextError):
                self.summary(**kwargs)
        with self.assertRaises(EditContextError):
            self.summary(include_visible_variables=True, variable_offset=999)
        original = build_edit_context(self.design, self.registry, self.targets)
        explicit = build_edit_context(self.design, self.registry, self.targets, response_detail="full", include_visible_variables=True, variable_offset=999)
        self.assertEqual(original, explicit)

    def test_legacy_value_summary_shape_is_unchanged_without_opt_in(self):
        for value in ("hello", {"key": "value"}, [1, 2]):
            self.assertNotIn("value_complete", value_summary(value))
            self.assertNotIn("value", value_summary(value))
        exact = value_summary({"enabled": False}, max_inline_bytes=1024)
        self.assertTrue(exact["value_complete"])
        self.assertEqual(exact["value"], {"enabled": False})

    def test_exact_inline_values_leave_signed_guard_and_expanded_operation_unchanged(self):
        with PayloadHarness() as harness:
            session_id = harness.service.begin_edit_session("ActionOne")["session_id"]
            context = harness.service.inspect_edit_context(session_id, [{"group_key": "main", "node_key": "target"}],
                response_detail="summary", parameter_paths=["/inputParams/settings"])
            node = context["nodes"][0]
            self.assertTrue(node["parameters"][0]["value_complete"])
            self.assertEqual(node["parameters"][0]["value"], {"flag": False, "notes": ""})
            template = node["edit_options"]["parameter_operations"][0]["template"]
            operation = {**template, "value": {"flag": True, "notes": ""}}
            session = harness.service.state.get_session(session_id)
            expanded = expand_guard_operations(session, harness.owner, harness.canvas, [operation], "synthetic-guard-secret")
            self.assertEqual(expanded[0]["expected_old_value"], {"flag": False, "notes": ""})
            self.assertNotIn("guard_ref", expanded[0])
            self.assertEqual(expanded[0]["value"], operation["value"])


if __name__ == "__main__":
    unittest.main()
