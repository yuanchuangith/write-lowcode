"""Read-only review behavior using synthetic canvases and bound local fixtures."""
from __future__ import annotations

from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from tests.helpers import baseline, transaction
from gxp_editor.approval_display import canvas_change_summary
from gxp_editor.component_preview import component_changes, control_flow_impacts, parameter_rows
from gxp_editor.logic_review_ui import LogicReview, display_value
from gxp_editor.qt_review_ui import ComponentReview
from gxp_editor.qt_theme import application


def canvas(nodes):
    return {"actionData": [{"key": "main", "title": "主动作", "actionType": "js", "data": nodes}], "globalVariables": []}


def assignment(key="assign", source="sourceA", label="局部变量-申请编号"):
    return {"key": key, "elementKey": "SetVariable", "title": "赋值", "depth": [], "paramsValue": {
        "inputParams": {"variableValue": {"paramTypes": "localVariable", "code": source, "value": "localVariable-" + source, "label": label}},
        "outputParams": {"variableName": {"code": "target", "label": "页面组件-申请编号-当前值"}}}}


class BusinessDisplayTests(unittest.TestCase):
    def test_boolean_to_number_is_a_real_parameter_and_control_change(self):
        condition = {"key": "if", "elementKey": "IfCondition", "paramsValue": {
            "inputParams": {"condition": {"Logic": "And", "Filters": [{"target": True, "equalTo": "Equal", "value": True}]}}}}
        child = assignment()
        child["depth"] = ["if"]
        before = canvas([condition, child])
        after = deepcopy(before)
        after["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["condition"]["Filters"][0]["value"] = 1
        changes = component_changes(before, after)
        self.assertEqual(len(changes), 1)
        self.assertTrue(parameter_rows(changes[0], {})[0]["changed"])
        self.assertEqual([item["node_key"] for item in control_flow_impacts(before, after)], ["assign"])

    def test_same_label_exposes_actual_expression_and_source_mode(self):
        before = canvas([assignment()])
        after = canvas([assignment(source="sourceB")])
        summary = canvas_change_summary(before, after)
        self.assertIn("sourceA", summary)
        self.assertIn("sourceB", summary)
        self.assertIn("局部变量", summary)

    def test_query_sources_are_separate_by_version_and_group(self):
        def query(key, name):
            return {"key": key, "elementKey": "QueryData", "paramsValue": {
                "inputParams": {"paramName": name}, "outputParams": {"parameterResults": {"code": "q"}}}}
        before = canvas([query("query", "oldPageParam"), assignment(source="q")])
        before["actionData"].append({"key": "other", "title": "其他分组", "data": [query("other-query", "otherPageParam")]})
        after = deepcopy(before)
        after["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["paramName"] = "newPageParam"
        after["actionData"][0]["data"][1] = assignment(source="q + 1")
        summary = canvas_change_summary(before, after)
        self.assertIn('修改前：直接使用页面参数“oldPageParam”', summary)
        self.assertNotIn('直接使用页面参数“newPageParam”', summary)
        self.assertNotIn('直接使用页面参数“otherPageParam”', summary)

    def test_later_and_sibling_scope_query_is_not_explained_as_source(self):
        before = canvas([assignment(source="q"), {"key": "later", "elementKey": "QueryData", "paramsValue": {
            "inputParams": {"paramName": "laterPageParam"}, "outputParams": {"parameterResults": {"code": "q"}}}}])
        after = deepcopy(before)
        after["actionData"][0]["data"][0] = assignment(source="q + 1")
        self.assertNotIn('直接使用页面参数“laterPageParam”', canvas_change_summary(before, after))

    def test_duplicate_query_outputs_keep_expression_instead_of_guessing_source(self):
        def query(key, parameter):
            return {"key": key, "elementKey": "QueryData", "paramsValue": {
                "inputParams": {"paramName": parameter}, "outputParams": {"parameterResults": {"code": "q"}}}}
        before = canvas([query("first", "firstPageParam"), query("second", "secondPageParam"), assignment(source="q")])
        after = deepcopy(before)
        after["actionData"][0]["data"][2] = assignment(source="q + 1")
        summary = canvas_change_summary(before, after)
        self.assertNotIn("直接使用页面参数", summary)
        self.assertIn("表达式：q", summary)
        self.assertIn("q + 1", summary)

    def test_native_else_chain_preserves_prior_branch_dependencies(self):
        def branch(key, element, depth=()):
            inputs = {} if element == "Else" else {"condition": {"code": key + "Amount >= 10", "paramTypes": "custom"}}
            return {"key": key, "elementKey": element, "depth": list(depth), "paramsValue": {"inputParams": inputs}}
        def child(key, parent):
            node = assignment(key)
            node["depth"] = [parent]
            return node
        # Native ElseIf/Else close the preceding branch, so their depth has
        # the previous branch key while their children have the new key.
        before = canvas([branch("if", "IfCondition"), child("if-child", "if"),
                         branch("elseif", "ElseIfCondition", ["if"]), child("elseif-child", "elseif"),
                         branch("else", "Else", ["elseif"]), child("else-child", "else"),
                         {"key": "end", "elementKey": "IfEnd", "depth": ["else"], "paramsValue": {}}, assignment("outside")])
        for controller, index, expected in (
                ("if", 0, {"if-child", "elseif", "elseif-child", "else", "else-child"}),
                ("elseif", 2, {"elseif-child", "else", "else-child"})):
            with self.subTest(controller=controller):
                after = deepcopy(before)
                after["actionData"][0]["data"][index]["paramsValue"]["inputParams"]["condition"]["code"] = controller + "Amount >= 20"
                self.assertEqual(len(component_changes(before, after)), 1)
                impacts = control_flow_impacts(before, after)
                self.assertEqual({item["node_key"] for item in impacts}, expected)
                for item in impacts:
                    self.assertEqual(item["kind"], "受条件影响")
                    self.assertEqual([source["node_key"] for source in item["affected_by"]], [controller])
                    self.assertFalse(item["affected_by"][0]["potential"])

    def test_loop_and_early_exit_impacts_remain_potential(self):
        for element in ("ForLoop", "ForEachArray", "ForEachObject", "ForEachDynamicArray", "WhileLoop"):
            with self.subTest(element=element):
                nested = assignment("body")
                nested["depth"] = ["loop"]
                before = canvas([{"key": "loop", "elementKey": element, "paramsValue": {"inputParams": {"count": 1}}},
                                 nested, {"key": "end", "elementKey": element + "End", "depth": ["loop"], "paramsValue": {}}, assignment("outside")])
                after = deepcopy(before)
                after["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["count"] = 2
                impacts = control_flow_impacts(before, after)
                self.assertEqual([item["node_key"] for item in impacts], ["body"])
                self.assertEqual(impacts[0]["kind"], "可能受控制流影响")
                self.assertTrue(impacts[0]["affected_by"][0]["potential"])
        before = canvas([assignment("before-exit"), {"key": "exit", "elementKey": "ExitAction", "paramsValue": {"inputParams": {"returnValue": 1}}}, assignment("after-exit")])
        after = deepcopy(before)
        after["actionData"][0]["data"][1]["paramsValue"]["inputParams"]["returnValue"] = 2
        impacts = control_flow_impacts(before, after)
        self.assertEqual([item["node_key"] for item in impacts], ["after-exit"])
        self.assertEqual(impacts[0]["kind"], "可能受控制流影响")
        self.assertEqual(impacts[0]["affected_by"][0]["node_key"], "exit")
        self.assertTrue(impacts[0]["affected_by"][0]["potential"])


class BusinessQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = application()
        cls.app.setQuitOnLastWindowClosed(False)

    def test_condition_impact_is_separate_and_click_locates_real_parent(self):
        condition = {"key": "if", "elementKey": "IfCondition", "title": "判断金额", "paramsValue": {
            "inputParams": {"condition": {"Logic": "And", "Filters": [{"target": {"label": "金额"}, "equalTo": "GreaterThanOrEqual", "value": 10}]}}}}
        child = assignment()
        child["depth"] = ["if"]
        closing = {"key": "end", "elementKey": "IfEnd", "depth": ["if"], "paramsValue": {}}
        before = canvas([condition, child, closing, assignment("outside")])
        after = deepcopy(before)
        after["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["condition"]["Filters"][0]["value"] = 20
        self.assertEqual(len(component_changes(before, after)), 1)
        self.assertEqual([item["node_key"] for item in control_flow_impacts(before, after)], ["assign"])
        preview = ComponentReview()
        self.addCleanup(preview.close)
        preview.render(before, after)
        self.assertIn("受条件影响", preview.lookup["after"][("main", "assign")].text(1))
        preview.select_canvas_node("main", "assign")
        self.assertEqual(preview.selected_change["kind"], "受条件影响")
        self.assertTrue(preview.control_button.isEnabled())
        preview.control_button.click()
        self.assertEqual(preview.selected_change["after"]["node"]["key"], "if")

    def test_derived_business_impact_locates_child_and_controller_without_mutating_report(self):
        condition = {"key": "if", "elementKey": "IfCondition", "title": "判断金额", "paramsValue": {
            "inputParams": {"condition": {"Logic": "And", "Filters": [{"target": {"code": "amount"}, "equalTo": "GreaterThanOrEqual", "value": 10}]}}}}
        child = assignment()
        child["depth"] = ["if"]
        before = canvas([condition, child])
        after = deepcopy(before)
        after["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["condition"]["Filters"][0]["value"] = 20
        report = {"changes": [{"group_key": "main", "node_key": "if", "field_path": "paramsValue/inputParams/condition/Filters/0/value",
                               "before": 10, "after": 20, "impact": "调整金额阈值"}]}
        original = deepcopy(report)
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.set_report(report, before=before, after=after)
        self.assertEqual(widget.changes.topLevelItemCount(), 2)
        derived = widget.changes.topLevelItem(1)
        self.assertIn("第 2 行", derived.text(0))
        self.assertIn("受条件影响", derived.text(3))
        self.assertIn("判断金额", derived.text(3))
        self.assertIn("待场景验证", derived.text(3))
        received = []
        widget.navigate_requested.connect(lambda *value: received.append(value))
        widget.changes.setCurrentItem(derived)
        self.assertTrue(widget.locate.isEnabled())
        self.assertTrue(widget.locate_controller.isEnabled())
        widget.locate.click()
        widget.locate_controller.click()
        self.assertEqual(received, [("main", "assign", ""), ("main", "if", "inputParams.condition")])
        self.assertEqual(report, original)

    def test_unknown_and_unassociated_requirements_are_never_shown_as_passed(self):
        widget = LogicReview()
        self.addCleanup(widget.close)
        report = {"status": "passed", "requirements": [{"id": "r1", "text": "未关联场景的要求", "status": "passed"}],
                  "scenarios": {"cases": [{"id": "c1", "expected": {"value": 1}, "candidate": {"status": "unverified", "actual": {}}, "baseline": {"status": "passed", "actual": {"value": 1}}}]}}
        original = deepcopy(report)
        widget.set_report(report)
        self.assertEqual(widget.requirements.topLevelItem(0).text(3), "待验证")
        self.assertEqual(widget.scenarios.topLevelItem(0).text(4), "待验证")
        self.assertIn("待验证", widget.status.text())
        self.assertEqual(report, original)

    def test_passed_cases_do_not_hide_pending_requirements_or_extra_changes(self):
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.set_report({"status": "unverified", "requirements": [{"id": "r1", "text": "尚未关联场景"}],
                           "changes": [{"group_key": "main", "node_key": "assign", "status": "unverified", "in_scope": False}],
                           "scenarios": {"cases": [{"id": "c1", "candidate": {"status": "passed", "actual": 1}}]}})
        self.assertIn("业务验收待验证", widget.status.text())
        self.assertIn("场景通过 1", widget.status.text())
        self.assertIn("需求待验证 1", widget.status.text())
        self.assertIn("额外变化待核对 1", widget.status.text())
        self.assertNotIn("所列场景通过", widget.status.text())
        self.assertIn("超出声明的需求范围", widget.changes.topLevelItem(0).text(3))

    def test_requirement_limit_is_not_overridden_by_passing_linked_case(self):
        widget = LogicReview()
        self.addCleanup(widget.close)
        report = {"status": "unverified", "requirements": [{"id": "r1", "text": "C# 查询只处理冻结资质",
            "status": "unverified", "case_ids": ["c1"], "reason": "C# 需求目标未执行验证"}],
            "scenarios": {"cases": [{"id": "c1", "requirement_ids": ["r1"],
                "candidate": {"status": "passed", "actual": True}}]}}
        original = deepcopy(report)
        widget.set_report(report)
        entry = widget.requirements.topLevelItem(0)
        self.assertEqual(entry.text(3), "待验证")
        self.assertIn("C#", entry.toolTip(3))
        self.assertEqual(widget.scenarios.topLevelItem(0).text(4), "通过")
        self.assertIn("需求待验证 1", widget.status.text())
        self.assertEqual(report, original)

    def test_target_navigation_preserves_nested_parameter_identity(self):
        widget = LogicReview()
        self.addCleanup(widget.close)
        changes = [{"group_key": "main", "node_key": "assign", "field_path": "paramsValue/inputParams/variableValue/code",
                    "before_rule": "old", "after_rule": "new", "impact": "仅改变来源"}]
        widget.set_report({"changes": changes})
        received = []
        widget.navigate_requested.connect(lambda *target: received.append(target))
        widget.changes.setCurrentItem(widget.changes.topLevelItem(0))
        widget.locate.click()
        self.assertEqual(received, [("main", "assign", "paramsValue/inputParams/variableValue/code")])

    def test_collapsed_evidence_retains_selection_and_tab_navigation_cannot_use_stale_target(self):
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.set_report({"changes": [{"group_key": "main", "node_key": "assign", "before": "old", "after": "new"}]})
        widget.changes.setCurrentItem(widget.changes.topLevelItem(0))
        evidence = widget.details.toPlainText()
        self.assertTrue(widget.details.isHidden())
        widget.details_toggle.click()
        self.assertFalse(widget.details.isHidden())
        self.assertEqual(widget.details.toPlainText(), evidence)
        widget.details_toggle.click()
        self.assertTrue(widget.details.isHidden())
        self.assertEqual(widget.details.toPlainText(), evidence)
        widget.evidence_tabs.setCurrentWidget(widget.requirements)
        self.assertFalse(widget.locate.isEnabled())
        self.assertIn("先选择", widget.locate.toolTip())
        widget.evidence_tabs.setCurrentWidget(widget.changes)
        self.assertTrue(widget.locate.isEnabled())
        self.assertEqual(widget.details.toPlainText(), evidence)

    def test_scenario_spec_inputs_and_requirement_target_remain_visible_and_navigable(self):
        widget = LogicReview()
        self.addCleanup(widget.close)
        target = {"group_key": "main", "node_key": "assign", "field_path": "paramsValue/inputParams/variableValue/code"}
        scenario_spec = {"id": "c1", "requirement_ids": ["r1"], "environment": {"pageParameters": {"applicationId": "REQ-001"}},
                         "steps": [{"event": "click", "component": "saveButton"}]}
        report = {"spec": {"requirements": [{"id": "r1", "text": "使用申请编号", "source_text": "读取页面参数", "targets": [target]}],
                           "scenarios": {"cases": [scenario_spec]}},
                  "scenarios": {"cases": [{"id": "c1", "expected": {"value": "REQ-001"},
                                            "baseline": {"status": "passed", "actual": {"value": "REQ-001"}},
                                            "candidate": {"status": "passed", "actual": {"value": "REQ-001"}, "trace": [{"api": "setValue"}]}}]}}
        original = deepcopy(report)
        widget.set_report(report)
        scenario = widget.scenarios.topLevelItem(0)
        self.assertIn("REQ-001", scenario.text(0))
        self.assertIn("click", scenario.text(0))
        self.assertIn("saveButton", scenario.text(0))
        self.assertEqual(widget.requirements.topLevelItem(0).text(3), "通过")
        received = []
        widget.navigate_requested.connect(lambda *value: received.append(value))
        widget.scenarios.setCurrentItem(scenario)
        self.assertIn("pageParameters", widget.details.toPlainText())
        self.assertIn("saveButton", widget.details.toPlainText())
        self.assertTrue(widget.locate.isEnabled())
        widget.locate.click()
        self.assertEqual(received, [("main", "assign", target["field_path"])])
        self.assertEqual(report, original)

    def test_explicit_null_is_distinct_from_missing_evidence(self):
        self.assertEqual(display_value(None), "null（空值）")
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.set_report({"changes": [{"group_key": "main", "node_key": "assign", "before": None, "after": None,
                                        "before_present": True, "after_present": False}], "scenarios": {"cases": [
            {"id": "null", "expected": None, "baseline": {"status": "passed", "actual": None}, "candidate": {"status": "unverified"}},
            {"id": "missing", "baseline": {}, "candidate": {}},
        ]}})
        explicit_null, missing = widget.scenarios.topLevelItem(0), widget.scenarios.topLevelItem(1)
        self.assertEqual(explicit_null.text(1), "null（空值）")
        self.assertIn("null（空值）", explicit_null.text(2))
        self.assertIn("未提供（待验证）", explicit_null.text(3))
        self.assertNotIn("null（空值）", explicit_null.text(3))
        self.assertEqual(missing.text(1), "未提供（待验证）")
        self.assertEqual(widget.changes.topLevelItem(0).text(1), "null（空值）")
        self.assertEqual(widget.changes.topLevelItem(0).text(2), "未提供（待验证）")

    def test_plan_viewer_is_file_only_without_approval_or_database_objects(self):
        from scripts import approval_app
        artifact = transaction()
        envelope = {"business_review": artifact["business_review"], "before": json.loads(baseline()["data"]),
                    "after": json.loads(artifact["desired_design"]["data"]), "plan_id": "fixture"}
        with patch.object(approval_app, "StateStore") as state, patch.object(approval_app, "load_config") as config, \
             patch.object(approval_app, "approve_statement") as approve, patch.object(approval_app, "execute_approved_transaction") as execute:
            window = approval_app.PlanReviewWindow(envelope)
            self.addCleanup(window.close)
            self.assertEqual(window.notebook.tabText(0), "业务逻辑验收")
            self.assertFalse(hasattr(window, "approve_button"))
            self.assertFalse(hasattr(window, "execute_button"))
            for tool in (state, config, approve, execute):
                tool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
