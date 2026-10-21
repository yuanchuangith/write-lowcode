from __future__ import annotations

import copy
import unittest

from tests.helpers import baseline  # Add mcp to the test path.
from gxp_editor.edit_context import EditContextError, build_edit_context


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
