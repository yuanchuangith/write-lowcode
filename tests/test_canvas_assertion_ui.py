"""Configuration evidence is visible without certifying business execution."""
from copy import deepcopy
import unittest

from gxp_editor.logic_review_ui import LogicReview
from gxp_editor.qt_theme import application


class CanvasAssertionDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = application()

    def fixture(self, status="passed"):
        target = {"group_key": "query", "node_key": "select", "element_key": "SelectData"}
        check = {"id": "keep-output", "requirement_ids": ["r"], "target": target,
            "path": "/outputParams/rows", "operator": "unchanged", "status": status,
            "reason": "配置与原值一致" if status == "passed" else "输出发生变化",
            "before_present": True, "after_present": True,
            "before": "qualifications", "after": "qualifications" if status == "passed" else "wrong"}
        report = {"status": "unverified" if status == "passed" else "failed",
            "requirements": [{"id": "r", "text": "保留查询输出", "source_text": "用户：保留原输出",
                "targets": [{"group_key": "query", "node_key": "select"}], "status": "unverified",
                "reason": "C# 业务执行待验证", "configuration_status": status}],
            "canvas_checks": [check], "scenarios": {"cases": []}}
        canvas = {"actionData": [{"key": "query", "title": "资质查询", "actionType": "csharp", "data": [
            {"key": "select", "title": "查询资质", "elementKey": "SelectData", "depth": [],
             "paramsValue": {"inputParams": {}, "outputParams": {"rows": "qualifications"}}}]}], "globalVariables": []}
        return report, canvas

    def test_configuration_pass_is_separate_from_pending_business_execution_and_navigable(self):
        report, canvas = self.fixture()
        original = deepcopy((report, canvas))
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.set_report(report, before=canvas, after=canvas)
        self.assertEqual(widget.requirements.topLevelItem(0).text(3), "待验证")
        self.assertEqual(widget.requirements.topLevelItem(0).text(4), "配置已核对")
        row = widget.configurations.topLevelItem(0)
        self.assertIn("第 1 行", row.text(0))
        self.assertIn("保持原值", row.text(1))
        self.assertEqual(row.text(2), "qualifications")
        self.assertIn("配置已核对", row.text(4))
        self.assertIn("业务验收待验证", widget.status.text())
        self.assertIn("配置通过 1", widget.status.text())
        received = []
        widget.navigate_requested.connect(lambda *args: received.append(args))
        widget.evidence_tabs.setCurrentWidget(widget.configurations)
        widget.configurations.setCurrentItem(row)
        widget.locate.click()
        self.assertEqual(received, [("query", "select", "paramsValue/outputParams/rows")])
        self.assertEqual((report, canvas), original)

    def test_failed_configuration_cannot_be_hidden_by_passing_scenario(self):
        report, canvas = self.fixture("failed")
        report["scenarios"]["cases"] = [{"id": "other", "candidate": {"status": "passed"}}]
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.set_report(report, before=canvas, after=canvas)
        self.assertIn("存在不符合预期", widget.status.text())
        self.assertIn("配置通过 0 / 失败 1", widget.status.text())
        self.assertEqual(widget.requirements.topLevelItem(0).text(4), "配置不符合预期")
        self.assertIn("wrong", widget.configurations.topLevelItem(0).text(3))

    def test_first_failed_configuration_is_selected_on_entry_and_manual_tabs_stay_selected(self):
        report, canvas = self.fixture()
        failed = deepcopy(report["canvas_checks"][0])
        failed.update(id="incorrect-output", status="failed", after="wrong", reason="输出发生变化")
        report["canvas_checks"].append(failed)
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.set_report(report, before=canvas, after=canvas)
        self.assertEqual(widget.views.currentIndex(), 1)
        self.assertIs(widget.evidence_tabs.currentWidget(), widget.configurations)
        self.assertIs(widget.configurations.currentItem(), widget.configurations.topLevelItem(1))
        self.assertEqual(widget.selected_payload["id"], "incorrect-output")
        self.assertTrue(widget.locate.isEnabled())
        widget.evidence_tabs.setCurrentWidget(widget.requirements)
        widget.views.setCurrentIndex(0)
        self.app.processEvents()
        self.assertEqual(widget.views.currentIndex(), 0)
        self.assertIs(widget.evidence_tabs.currentWidget(), widget.requirements)

    def test_failed_scenario_is_selected_even_after_an_earlier_improvement(self):
        report, canvas = self.fixture()
        report["scenarios"]["cases"] = [
            {"id": "improved", "baseline": {"status": "failed"}, "candidate": {"status": "passed"}},
            {"id": "regression", "targets": [{"group_key": "query", "node_key": "select"}],
             "baseline": {"status": "passed"}, "candidate": {"status": "failed"}}]
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.set_report(report, before=canvas, after=canvas)
        self.assertEqual(widget.views.currentIndex(), 1)
        self.assertIs(widget.evidence_tabs.currentWidget(), widget.scenarios)
        self.assertIs(widget.scenarios.currentItem(), widget.scenarios.topLevelItem(1))
        self.assertEqual(widget.selected_payload["id"], "regression")
        self.assertEqual(widget.flow.case_picker.currentIndex(), 1)
        self.assertTrue(widget.locate.isEnabled())
        widget.flow.case_picker.setCurrentIndex(0)
        self.app.processEvents()
        self.assertEqual(widget.flow.case_picker.currentIndex(), 0)

    def test_successful_report_keeps_existing_default_view(self):
        report, canvas = self.fixture()
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.set_report(report, before=canvas, after=canvas)
        self.assertEqual(widget.views.currentIndex(), 0)
        self.assertIs(widget.evidence_tabs.currentWidget(), widget.changes)
        self.assertIsNone(widget.selected_payload)

    def test_strict_scope_failure_opens_first_failed_change_after_other_failure_priorities(self):
        report, canvas = self.fixture()
        report["status"] = "failed"
        report["changes"] = [
            {"group_key": "query", "node_key": "select", "field_path": "paramsValue/inputParams/first", "status": "covered"},
            {"group_key": "query", "node_key": "select", "field_path": "paramsValue/outputParams/rows", "status": "failed", "in_scope": False}]
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.set_report(report, before=canvas, after=canvas)
        self.assertEqual(widget.views.currentIndex(), 1)
        self.assertIs(widget.evidence_tabs.currentWidget(), widget.changes)
        self.assertIs(widget.changes.currentItem(), widget.changes.topLevelItem(1))
        self.assertEqual(widget.selected_payload["field_path"], "paramsValue/outputParams/rows")
        self.assertTrue(widget.locate.isEnabled())
        report["canvas_checks"][0]["status"] = "failed"
        widget.set_report(report, before=canvas, after=canvas)
        self.assertIs(widget.evidence_tabs.currentWidget(), widget.configurations)

    def test_manual_scope_unknown_does_not_change_initial_view(self):
        report, canvas = self.fixture()
        report["changes"] = [{"group_key": "query", "node_key": "select", "status": "unverified", "in_scope": False}]
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.set_report(report, before=canvas, after=canvas)
        self.assertEqual(widget.views.currentIndex(), 0)

    def test_passing_partial_checks_do_not_hide_unverified_requirement_scope(self):
        report, canvas = self.fixture()
        report["requirements"][0].update(configuration_status="unverified",
            configuration_reason="仅关联断言通过，另一个查询尚未核对")
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.set_report(report, before=canvas, after=canvas)
        entry = widget.requirements.topLevelItem(0)
        self.assertEqual(entry.text(4), "配置待验证")
        self.assertIn("另一个查询尚未核对", entry.toolTip(4))
        self.assertIn("不代表真实业务运行", entry.toolTip(4))
        self.assertIn("配置已核对", widget.configurations.topLevelItem(0).text(4))

    def test_new_report_clears_configuration_evidence_and_distinguishes_missing_from_null(self):
        report, canvas = self.fixture()
        check = report["canvas_checks"][0]
        check.update(operator="equals", expected=None, before_present=False, before=None, after=None)
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.set_report(report, before=canvas, after=canvas)
        row = widget.configurations.topLevelItem(0)
        self.assertIn("未提供", row.text(2))
        self.assertIn("null", row.text(3))
        widget.evidence_tabs.setCurrentWidget(widget.configurations)
        widget.configurations.setCurrentItem(row)
        self.assertTrue(widget.locate.isEnabled())
        widget.set_report({})
        self.assertEqual(widget.configurations.topLevelItemCount(), 1)
        self.assertIn("尚未声明", widget.configurations.topLevelItem(0).text(0))
        self.assertFalse(widget.locate.isEnabled())
        self.assertNotIn("配置通过", widget.status.text())


if __name__ == "__main__":
    unittest.main()
