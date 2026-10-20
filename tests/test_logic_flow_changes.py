"""Configuration comparisons show concrete edits and retain their native paths."""
from copy import deepcopy
import unittest

from gxp_editor.logic_flow_model import MISSING, flow_comparison, same


TARGET = {"group_key": "main", "node_key": "query"}


def sample_query():
    parent = {"key": "if", "elementKey": "IfCondition", "paramsValue": {"inputParams": {
        "condition": {"Logic": "And", "Filters": [{"target": {"code": "settings"}, "equalTo": "NotEqualNull"}]}}}}
    query = {"key": "query", "title": "查询数据", "elementKey": "SelectData", "depth": ["if"],
             "paramsValue": {"inputParams": {
                 "modelName": {"modelName": "qualification", "modelComment": "资质表列表", "modelId": "model"},
                 "selectDataConfig": {"whereConditions": {"Logic": "And", "Filters": [
                     {"Field": "position_id", "Operator": "Equal", "value": "position_id",
                      "ParamInput": {"code": "position_id", "label": "岗位编号"}}]}, "sort": []}},
                 "outputParams": {"rows": {"code": "qualifications", "label": "qualifications"}}}}
    return {"actionData": [{"key": "main", "title": "资质处理", "data": [parent, query]}]}


def input_params(data):
    return data["actionData"][0]["data"][1]["paramsValue"]["inputParams"]


class FlowComparisonTests(unittest.TestCase):
    def test_added_query_filters_are_concrete_operation_edits_only(self):
        before = sample_query()
        after = deepcopy(before)
        filters = input_params(after)["selectDataConfig"]["whereConditions"]["Filters"]
        filters += [
            {"Field": "isDeleted", "Operator": "Equal", "value": "0", "type": "boolean",
             "ParamInput": {"code": "0", "label": "未删除", "paramTypes": "custom", "dataType": "boolean"}},
            {"Field": "status", "Operator": "Equal", "value": '"freeze"', "type": "text",
             "ParamInput": {"code": '"freeze"', "label": "冻结(freeze)", "paramTypes": "custom", "dataType": "string"}},
        ]
        result = flow_comparison(before, after, TARGET)
        self.assertEqual([row["kind"] for row in result["changes"]], ["新增", "新增"])
        self.assertEqual([row["after"] for row in result["changes"]],
                         ["isDeleted = 0（未删除）", 'status = "freeze"（冻结(freeze)）'])
        self.assertEqual(result["changes"][0]["after_value"], filters[1])
        for index, row in enumerate(result["changes"], 1):
            self.assertEqual(row["path"], f"inputParams.selectDataConfig.whereConditions.Filters[{index}]")
            self.assertEqual(row["target"], {**TARGET, "field_path": row["path"]})
            self.assertIn("筛选条件", row["field"])
            self.assertNotIn("inputParams", row["field"])
        for version in ("baseline", "candidate"):
            self.assertEqual([len(card["changes"]) for card in result[version]["cards"]], [0, 0, 2, 0])
        self.assertEqual(result["candidate"]["cards"][0]["title"], "资质表列表")
        self.assertEqual(result["candidate"]["cards"][0]["body"], "qualification")
        self.assertEqual(result["candidate"]["cards"][3]["title"], "qualifications")

    def test_same_label_source_swap_is_not_hidden_or_repeated_on_operation(self):
        before = sample_query()
        node = before["actionData"][0]["data"][1]
        node["elementKey"] = "SetVariableValue"
        node["paramsValue"] = {"inputParams": {"attributeName": "value",
            "variableName": {"code": 'inbiz("destination")', "label": "目标"},
            "attributeValue": {"code": 'inbiz("old").value', "label": "相同名称"}}}
        after = deepcopy(before)
        input_params(after)["attributeValue"]["code"] = 'inbiz("new").value'
        result = flow_comparison(before, after, TARGET)
        self.assertEqual(len(result["changes"]), 1)
        self.assertEqual(result["changes"][0]["role"], "数据来源")
        self.assertIn('inbiz("old").value', result["changes"][0]["before"])
        self.assertIn('inbiz("new").value', result["changes"][0]["after"])
        old, new = result["baseline"]["cards"], result["candidate"]["cards"]
        self.assertFalse(same(old[0]["identity"], new[0]["identity"]))
        self.assertTrue(same(old[2]["identity"], new[2]["identity"]))

    def test_parent_if_edit_affects_condition_card_and_locates_parent(self):
        before = sample_query()
        after = deepcopy(before)
        parent = after["actionData"][0]["data"][0]
        parent["paramsValue"]["inputParams"]["condition"]["Filters"][0]["target"]["code"] = "otherSettings"
        result = flow_comparison(before, after, TARGET)
        self.assertEqual(len(result["changes"]), 1)
        self.assertEqual(result["changes"][0]["role"], "执行条件")
        self.assertEqual(result["changes"][0]["target"]["node_key"], "if")
        self.assertEqual([len(card["changes"]) for card in result["candidate"]["cards"]], [0, 1, 0, 0])
        for index in (0, 2, 3):
            self.assertTrue(same(result["baseline"]["cards"][index]["identity"],
                                 result["candidate"]["cards"][index]["identity"]))

    def test_long_values_missing_null_empty_and_false_remain_distinct(self):
        before = sample_query()
        after = deepcopy(before)
        input_params(before)["custom"] = {"removed": False, "null": None, "empty": "", "boolean": False, "long": "old"}
        long_value = "完整表达式\n" + "a" * 3000
        input_params(after)["custom"] = {"added": None, "null": "", "empty": False, "boolean": None, "long": long_value}
        result = flow_comparison(before, after, TARGET)
        rows = {row["path"].rsplit(".", 1)[-1]: row for row in result["changes"]}
        self.assertEqual(rows["long"]["after"], long_value)
        self.assertEqual(rows["removed"]["kind"], "删除")
        self.assertIs(rows["added"]["before_value"], MISSING)
        self.assertEqual(rows["added"]["after"], "空值 null")
        self.assertNotEqual(rows["added"]["before"], rows["added"]["after"])
        self.assertEqual(rows["null"]["after"], '空字符串 ""')
        self.assertEqual(rows["empty"]["after"], "否 false")
        self.assertEqual(rows["boolean"]["before"], "否 false")

    def test_output_mapping_is_not_marked_as_query_operation_change(self):
        before = sample_query()
        input_params(before)["selectDataConfig"]["outputMapping"] = {"id": "old"}
        after = deepcopy(before)
        input_params(after)["selectDataConfig"]["outputMapping"]["id"] = "new"
        result = flow_comparison(before, after, TARGET)
        self.assertEqual([row["role"] for row in result["changes"]], ["流向目标"])
        self.assertTrue(same(result["baseline"]["cards"][2]["identity"],
                             result["candidate"]["cards"][2]["identity"]))

    def test_unchanged_document_has_no_changes_and_is_not_mutated(self):
        before = sample_query()
        snapshot = deepcopy(before)
        result = flow_comparison(before, deepcopy(before), TARGET)
        self.assertEqual(result["changes"], [])
        self.assertEqual(before, snapshot)
        self.assertTrue(all(card["changes"] == [] for card in result["baseline"]["cards"]))

    def test_parameter_changes_precede_generated_descriptions(self):
        before = sample_query()
        after = deepcopy(before)
        input_params(after)["selectDataConfig"]["sort"] = [{"Field": "id", "direction": "asc"}]
        after["actionData"][0]["data"][1]["description"] = "说明" * 1000
        rows = flow_comparison(before, after, TARGET)["changes"]
        self.assertIn("selectDataConfig", rows[0]["path"])
        self.assertEqual(rows[-1]["path"], "description")

    def test_description_html_is_readable_while_raw_values_are_retained(self):
        before = sample_query()
        old_html = '<div data-action="old"><code id="a">isDeleted &lt; 1 &amp;&amp; status</code></div>'
        new_html = '<div data-action="new"><code id="b">isDeleted &lt; 1 &amp;&amp; status</code></div>'
        before["actionData"][0]["data"][1]["description"] = old_html
        after = deepcopy(before)
        after["actionData"][0]["data"][1]["description"] = new_html
        rows = flow_comparison(before, after, TARGET)["changes"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["before"], "isDeleted < 1 && status")
        self.assertEqual(row["after"], row["before"])
        self.assertNotIn("<code", row["after"])
        self.assertNotIn("data-action", row["after"])
        self.assertIn("仅说明格式变化", row["field"])
        self.assertEqual(row["before_value"], old_html)
        self.assertEqual(row["after_value"], new_html)


if __name__ == "__main__":
    unittest.main()
