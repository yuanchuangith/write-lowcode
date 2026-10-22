from __future__ import annotations

import copy
import json
import unittest

from tests.helpers import baseline  # Add mcp to the test path.
from gxp_editor.edit_context import EditContextError, build_edit_context
from gxp_editor.operation_preflight import preflight_batch


def declaration(key, name, depth=None):
    return {"key": key, "title": name, "elementKey": "SetVariable", "depth": depth or [], "paramsValue": {
        "inputParams": {"variableType": "string", "variableValue": {"paramTypes": "custom", "value": "''", "code": "''"}},
        "outputParams": {"variableName": {"paramTypes": "custom", "value": name, "code": name, "dataType": "string"}}}}


class EditContextTests(unittest.TestCase):
    def setUp(self):
        self.registry = {"elements": [
            {"key": "SetVariable", "name": "声明变量", "input_params": [{"key": "variableType", "defaultValue": "string"}],
             "output_params": [{"key": "variableName"}]},
            {"key": "If", "name": "条件", "level_marker": True},
            {"key": "Noop", "name": "目标", "input_params": [], "output_params": []}]}
        condition = {"key": "condition", "title": "条件判断", "elementKey": "If", "depth": [],
                     "paramsValue": {"inputParams": {"condition": "pageMode == add"}, "outputParams": {}}}
        target = {"key": "target", "title": "目标元件", "elementKey": "Noop", "depth": ["condition"],
                  "paramsValue": {"inputParams": {"x": None}, "outputParams": {}, "nativeExtra": {"keep": True}}}
        self.design = {"actionData": [{"key": "main", "title": "主动作", "actionType": "js",
            "inputParams": [{"key": "input", "title": "request", "type": "string"}],
            "outputParams": [{"key": "output", "title": "result", "type": "object"}],
            "data": [declaration("root", "rootValue"), condition,
                     declaration("branch", "branchValue", ["condition"]), target,
                     declaration("future", "futureValue", ["condition"])]}],
            "globalVariables": [{"name": "globalValue", "dataType": "number"}]}
        self.targets = [{"group_key": "main", "node_key": "target"}]

    def build(self):
        return build_edit_context(self.design, self.registry, self.targets)

    def test_exact_native_values_parent_neighbors_and_line_are_exposed_without_mutation(self):
        original = copy.deepcopy((self.design, self.registry, self.targets))
        context = self.build()
        node = context["nodes"][0]
        self.assertEqual(node["line"], 4)
        self.assertEqual(node["group_title"], "主动作")
        self.assertEqual(node["language"], "js")
        self.assertEqual(node["params_value"], self.design["actionData"][0]["data"][3]["paramsValue"])
        self.assertTrue(node["params_value_present"])
        self.assertEqual(node["previous"]["node_key"], "branch")
        self.assertEqual(node["next"]["node_key"], "future")
        self.assertEqual(node["parents"][0]["params_value"]["inputParams"]["condition"], "pageMode == add")
        self.assertEqual(node["parents"][0]["line"], 2)
        self.assertEqual(node["native_contract"], self.registry["elements"][2])
        self.assertTrue(context["read_only"])
        self.assertTrue(node["source_evidence_required"])
        self.assertIn("not parsed", node["source_evidence_hint"])
        node["params_value"]["inputParams"]["x"] = "display-only-change"
        self.assertEqual((self.design, self.registry, self.targets), original)

    def test_visible_variables_follow_native_declaration_order_and_scope(self):
        node = self.build()["nodes"][0]
        variables = {item["name"]: item for item in node["visible_variables"]}
        self.assertEqual(set(variables), {"request", "result", "globalValue", "rootValue", "branchValue"})
        self.assertEqual(variables["branchValue"]["source"]["node_key"], "branch")
        self.assertEqual(variables["rootValue"]["reference"]["dataType"], "string")
        self.design["actionData"][0]["data"][3]["depth"] = []
        variables = {item["name"] for item in self.build()["nodes"][0]["visible_variables"]}
        self.assertNotIn("branchValue", variables)
        self.assertNotIn("futureValue", variables)

    def test_csharp_scope_uses_native_reference_codes(self):
        self.design["actionData"][0]["actionType"] = "csharp"
        variables = {item["name"]: item for item in self.build()["nodes"][0]["visible_variables"]}
        self.assertEqual(variables["result"]["reference"]["code"], 'outputParams["result"]')
        self.assertEqual(variables["globalValue"]["reference"]["code"], 'this._service.TryGetValue("globalVariables.globalValue")')

    def test_absent_parameters_and_edge_neighbors_remain_explicit(self):
        first = self.design["actionData"][0]["data"][0]
        del first["paramsValue"]
        self.targets = [{"group_key": "main", "node_key": "root"}, {"group_key": "main", "node_key": "future"}]
        nodes = self.build()["nodes"]
        self.assertFalse(nodes[0]["params_value_present"])
        self.assertIsNone(nodes[0]["params_value"])
        self.assertIsNone(nodes[0]["previous"])
        self.assertIsNone(nodes[1]["next"])

    def test_invalid_targets_unknown_elements_and_ambiguity_fail(self):
        for targets in (None, [], self.targets * 2, self.targets * 11,
                        [{"group_key": "main", "node_key": "unknown"}],
                        [{"group_key": "unknown", "node_key": "target"}],
                        [{"group_key": "main", "node_key": "target", "extra": True}],
                        [{"group_key": "main", "node_key": ""}]):
            with self.subTest(targets=targets):
                with self.assertRaises(EditContextError):
                    build_edit_context(self.design, self.registry, targets)
        self.design["actionData"][0]["data"][3]["elementKey"] = "Unknown"
        with self.assertRaisesRegex(EditContextError, "Unknown native element"):
            self.build()

    def test_malformed_arrays_params_depth_and_duplicate_keys_fail(self):
        mutations = [lambda d, r: d.update(actionData={}),
                     lambda d, r: d.update(globalVariables={}),
                     lambda d, r: d["actionData"][0].update(data={}),
                     lambda d, r: d["actionData"][0].update(inputParams={}),
                     lambda d, r: d["actionData"][0]["data"][3].update(paramsValue=[]),
                     lambda d, r: d["actionData"][0]["data"][3]["paramsValue"].update(inputParams=[]),
                     lambda d, r: d["actionData"][0]["data"][3].update(depth="condition"),
                     lambda d, r: d["actionData"][0]["data"][3].update(depth=["future"]),
                     lambda d, r: d["actionData"][0]["data"][3].update(depth=["condition", "condition"]),
                     lambda d, r: d["actionData"].append(copy.deepcopy(d["actionData"][0])),
                     lambda d, r: d["actionData"][0]["data"].append(copy.deepcopy(d["actionData"][0]["data"][0])),
                     lambda d, r: r["elements"].append(copy.deepcopy(r["elements"][0])),
                     lambda d, r: r["elements"][2].update(input_params={})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                design, registry = copy.deepcopy((self.design, self.registry))
                mutate(design, registry)
                with self.assertRaises(EditContextError):
                    build_edit_context(design, registry, self.targets)

    def test_large_complete_evidence_is_rejected_without_truncation(self):
        self.design["actionData"][0]["data"][3]["paramsValue"]["large"] = "值" * 400000
        with self.assertRaisesRegex(EditContextError, "1 MiB.*reduce targets"):
            self.build()

    def test_parameter_templates_reference_exact_old_values_without_duplicate_payloads(self):
        node = self.build()["nodes"][0]
        template = node["edit_options"]["parameter_operations"][0]
        self.assertFalse(template["complete"])
        self.assertEqual(template["template"], {"operation": "set_parameter", "action_group_key": "main",
            "target_node_key": "target", "expected_element_key": "Noop", "path": "/inputParams/x"})
        self.assertEqual(template["required_inputs"], ["expected_old_value", "value"])
        self.assertEqual(template["expected_old_value_ref"], {"node_key": "target", "field": "params_value", "path": "/inputParams/x"})
        self.assertNotIn("expected_old_value", template["template"])

    def test_atomic_paraminput_paths_and_unique_native_row_templates(self):
        self.targets = [{"group_key": "main", "node_key": "root"}]
        options = self.build()["nodes"][0]["edit_options"]
        paths = [item["template"]["path"] for item in options["parameter_operations"]]
        self.assertIn("/inputParams/variableValue", paths)
        self.assertNotIn("/inputParams/variableValue/code", paths)
        self.assertEqual(options["atomic_parameter_paths"], ["/inputParams/variableValue", "/outputParams/variableName"])
        self.targets = [{"group_key": "main", "node_key": "target"}]
        self.design["actionData"][0]["data"][3]["paramsValue"]["inputParams"]["rows"] = [{"id": "unique", "value": 1}, {"id": "repeat"}, {"id": "repeat"}]
        templates = self.build()["nodes"][0]["edit_options"]["parameter_operations"]
        rows = [item for item in templates if item["template"]["operation"] == "set_array_item"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["template"]["item_key"], {"field": "id", "value": "unique"})
        self.assertFalse(any("/rows/0" in item["template"]["path"] for item in templates))

    def test_template_pagination_is_explicit_and_context_evidence_stays_complete(self):
        params = self.design["actionData"][0]["data"][3]["paramsValue"]["inputParams"]
        params.update({"p" + str(index): index for index in range(90)})
        first = self.build()
        self.assertEqual(first["operation_page"]["returned"], 64)
        self.assertFalse(first["operation_page"]["complete"])
        self.assertEqual(first["next_operation_cursor"], 64)
        self.assertEqual(first["nodes"][0]["params_value"]["inputParams"], params)
        second = build_edit_context(self.design, self.registry, self.targets, operation_cursor=first["next_operation_cursor"])
        self.assertTrue(second["operation_page"]["complete"])
        self.assertIsNone(second["next_operation_cursor"])
        all_paths = [item["template"]["path"] for page in (first, second) for item in page["nodes"][0]["edit_options"]["parameter_operations"]]
        self.assertEqual(len(all_paths), len(set(all_paths)))
        self.assertEqual(len(all_paths), 91)
        with self.assertRaises(EditContextError):
            build_edit_context(self.design, self.registry, self.targets, operation_cursor=1000)

    def test_continuation_pages_send_only_exact_guards_and_repeat_deterministically(self):
        params = self.design["actionData"][0]["data"][3]["paramsValue"]["inputParams"]
        params.update({"p" + str(index): {"value": index} for index in range(40)})
        original = copy.deepcopy((self.design, self.registry))
        second = build_edit_context(self.design, self.registry, self.targets, operation_cursor=64)
        self.assertEqual(second["context_detail"], "compact_guards")
        node = second["nodes"][0]
        self.assertEqual(set(second["omitted_fields"]),
                         {"params_value", "parents", "visible_variables", "native_contract", "operation_contracts"})
        for name in ("params_value", "parents", "visible_variables", "native_contract"):
            self.assertNotIn(name, node)
        self.assertNotIn("operation_contracts", second)
        for row in node["edit_options"]["parameter_operations"]:
            guard = node["parameter_guards"][row["expected_old_value_ref"]["guard_index"]]
            self.assertEqual(guard["path"], row["template"]["path"])
            value = params
            for key in guard["path"].split("/")[2:]:
                value = value[key]
            self.assertEqual(guard["value"], value)
        repeated = build_edit_context(self.design, self.registry, self.targets, operation_cursor=64)
        self.assertEqual(repeated, second)
        node["parameter_guards"][0]["value"] = "display change"
        self.assertEqual((self.design, self.registry), original)
        full = build_edit_context(self.design, self.registry, self.targets, operation_cursor=64, include_context=True)
        self.assertEqual(full["context_detail"], "full_context")
        self.assertEqual(full["nodes"][0]["params_value"]["inputParams"], params)

    def test_compact_guards_preserve_escaped_paths_and_complete_array_records(self):
        params = self.design["actionData"][0]["data"][3]["paramsValue"]["inputParams"]
        params.update({"a/b": {"~name": "exact"}, "rows": [{"id": 1.0, "key": "one", "value": False}]})
        result = build_edit_context(self.design, self.registry, self.targets, include_context=False)
        node = result["nodes"][0]
        by_path = {item["path"]: item for item in node["parameter_guards"] if "item_key" not in item}
        self.assertEqual(by_path["/inputParams/a~1b/~0name"]["value"], "exact")
        array_guards = [item for item in node["parameter_guards"] if "item_key" in item]
        self.assertEqual(len(array_guards), 2)
        self.assertTrue(all(item["value"] == params["rows"][0] for item in array_guards))
        self.assertIs(array_guards[0]["value"]["value"], False)
        self.assertIsInstance(array_guards[0]["value"]["id"], float)
        operations = []
        for row in node["edit_options"]["parameter_operations"]:
            guard = node["parameter_guards"][row["expected_old_value_ref"]["guard_index"]]
            operations.append({**row["template"], "expected_old_value": copy.deepcopy(guard["value"]),
                               "value": copy.deepcopy(guard["value"])})
        report = preflight_batch(self.design, operations)
        self.assertEqual(report["issues"], [])
        self.assertTrue(report["all_no_change"])
        row = node["edit_options"]["parameter_operations"][0]
        invalid = {**row["template"], "expected_old_value": row["expected_old_value_ref"], "value": None}
        self.assertEqual(preflight_batch(self.design, [invalid])["issues"][0]["reason_code"], "old_value_mismatch")

    def test_compact_fallback_omits_oversized_unrelated_context_without_truncating_guards(self):
        self.design["actionData"][0]["data"][3]["paramsValue"]["unrelated"] = "x" * (1024 * 1024)
        with self.assertRaises(EditContextError) as caught:
            self.build()
        self.assertEqual(caught.exception.details["reason_code"], "edit_context_response_limit")
        self.assertIn("include_context=false", caught.exception.details["next_step"])
        compact = build_edit_context(self.design, self.registry, self.targets, include_context=False)
        self.assertLess(len(json.dumps(compact)), 16384)
        self.assertEqual(compact["nodes"][0]["parameter_guards"][0]["value"], None)
        self.design["actionData"][0]["data"][3]["paramsValue"]["inputParams"]["x"] = "y" * (1024 * 1024)
        with self.assertRaises(EditContextError) as caught:
            build_edit_context(self.design, self.registry, self.targets, include_context=False)
        self.assertEqual(caught.exception.details["context_detail"], "compact_guards")
        self.assertIn("No parameter or guard was truncated", caught.exception.details["next_step"])
        self.assertNotIn("yyyy", str(caught.exception.details))

    def test_context_detail_flag_requires_boolean(self):
        for value in (0, 1, "true", []):
            with self.subTest(value=value), self.assertRaisesRegex(EditContextError, "include_context"):
                build_edit_context(self.design, self.registry, self.targets, include_context=value)

    def test_native_open_block_offers_only_incomplete_insertion_template(self):
        self.targets = [{"group_key": "main", "node_key": "condition"}]
        insertion = self.build()["nodes"][0]["edit_options"]["insert_in_block"]
        self.assertTrue(insertion["available"])
        self.assertEqual(insertion["required_inputs"], ["element_key"])
        self.assertFalse(insertion["complete"])
        self.assertTrue(insertion["native_validation_required"])
