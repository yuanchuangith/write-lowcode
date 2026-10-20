"""Graphical review keeps configuration, observed values and coverage distinct."""
from copy import deepcopy
import gc
import unittest

from PySide6.QtCore import Qt, QUrl
from PySide6.QtTest import QTest

from gxp_editor.logic_flow_model import (
    MISSING, UNDEFINED, condition_text, field_states, flow_lane, flow_options, output_states, same, trace_events,
)
from gxp_editor.logic_review_ui import LogicReview
from gxp_editor.logic_flow_ui import FlowCard, difference_html
from gxp_editor.qt_theme import application


def sample():
    condition = {"key": "if", "elementKey": "IfCondition", "title": "判断新增", "paramsValue": {
        "inputParams": {"condition": {"Logic": "And", "Filters": [{
            "target": {"paramTypes": "custom", "code": "true"}, "equalTo": "Equal", "value": True}]}}}}
    assignment = {"key": "assign", "elementKey": "SetVariableValue", "title": "回填编号", "depth": ["if"],
                  "paramsValue": {"inputParams": {"attributeName": "value",
                      "variableName": {"code": 'inbiz("target")', "label": "申请编号"},
                      "attributeValue": {"code": 'inbiz("source").value', "label": "来源编号"}}}}
    before = {"actionData": [{"key": "main", "title": "初始化", "data": [condition, assignment]}]}
    after = deepcopy(before)
    after["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["condition"]["Filters"][0]["target"]["code"] = 'inbiz.getPageMode() === "add"'
    spec = {"id": "edit", "title": "编辑保留编号", "requirement_ids": ["retain"], "environment": {
        "page_mode": "edit", "fields": {"source": {"value": "A-001"}, "target": {"value": "EDIT-008"}}},
        "steps": [{"kind": "call_group", "group_key": "main"}], "expect": {"fields": {"target": {"value": "EDIT-008"}}}}
    case = {"id": "edit", "title": spec["title"], "expected": spec["expect"],
            "baseline": {"status": "failed", "actual": {"fields": {"source": {"value": "A-001"}, "target": {"value": "A-001"}}},
                "trace": [{"api": "group.main", "step": 0}, {"api": "source.value.read", "value": "A-001"}, {"api": "target.value.write", "value": "A-001"}],
                "assertions": [{"name": "fields.target.value", "passed": False}]},
            "candidate": {"status": "passed", "actual": {"fields": {"source": {"value": "A-001"}, "target": {"value": "EDIT-008"}}},
                "trace": [{"api": "group.main", "step": 0}, {"api": "inbiz.getPageMode", "value": "edit"}],
                "assertions": [{"name": "fields.target.value", "passed": True}]}}
    report = {"status": "passed", "spec": {"requirements": [{"id": "retain", "text": "编辑时保留编号", "source_text": "编辑时保留编号"}],
               "scenarios": {"cases": [spec]}}, "scenarios": {"cases": [case]},
              "changes": [{"group_key": "main", "node_key": "if", "before": True, "after": False}]}
    return before, after, report, case, spec


class FlowModelTests(unittest.TestCase):
    def test_same_input_reveals_overwrite_before_and_preservation_after(self):
        _, _, _, case, spec = sample()
        row = field_states(case, spec)[0]
        self.assertEqual(row["id"], "target")
        self.assertEqual((row["baseline"]["value"], row["baseline"]["behavior"], row["baseline"]["verdict"]), ("A-001", "覆盖原值", "failed"))
        self.assertEqual((row["candidate"]["value"], row["candidate"]["behavior"], row["candidate"]["verdict"]), ("EDIT-008", "保留原值", "passed"))
        self.assertEqual((row["baseline"]["writes"], row["candidate"]["writes"]), (1, 0))

    def test_same_value_write_is_not_reported_as_untouched(self):
        _, _, _, case, spec = sample()
        case["candidate"]["trace"].append({"api": "target.value.write", "value": "EDIT-008"})
        self.assertEqual(field_states(case, spec)[0]["candidate"]["behavior"], "同值写入")

    def test_null_missing_undefined_and_boolean_number_do_not_collapse(self):
        self.assertFalse(same(True, 1))
        self.assertFalse(same(MISSING, None))
        self.assertFalse(same(UNDEFINED, MISSING))
        _, _, _, case, spec = sample()
        case["candidate"]["actual"]["fields"]["target"] = {}
        case["candidate"]["actual"]["field_value_presence"] = {"target": "undefined"}
        self.assertIs(field_states(case, spec)[0]["candidate"]["value"], UNDEFINED)

    def test_unknown_execution_cannot_make_a_matching_field_green(self):
        _, _, _, case, spec = sample()
        case["candidate"]["status"] = "unverified"
        row = field_states(case, spec)[0]
        self.assertEqual(row["candidate"]["verdict"], "unverified")
        self.assertNotEqual(row["candidate"]["behavior"], "保留原值")

    def test_failed_execution_needs_individual_assertion_before_green(self):
        _, _, _, case, spec = sample()
        case["candidate"]["status"] = "failed"
        case["candidate"]["assertions"] = []
        self.assertEqual(field_states(case, spec)[0]["candidate"]["verdict"], "unverified")
        case["candidate"]["assertions"] = [{"name": "fields.target.value", "passed": True}]
        self.assertEqual(field_states(case, spec)[0]["candidate"]["verdict"], "passed")

    def test_external_input_updates_are_not_attributed_to_generated_code(self):
        _, _, _, case, spec = sample()
        spec["steps"].insert(0, {"kind": "set_inputs", "fields": {"target": {"value": "EDIT-008"}}})
        self.assertEqual(field_states(case, spec)[0]["candidate"]["behavior"], "含外部输入变化")

    def test_selected_rows_and_display_text_are_separate_observations(self):
        _, _, _, case, spec = sample()
        spec["environment"]["fields"]["source"] = {"selected_rows": [{"id": 7}], "text": "合格"}
        for version in ("baseline", "candidate"):
            case[version]["actual"]["fields"]["source"] = {"selected_rows": [{"id": 7}], "text": "合格"}
            case[version]["trace"] = [{"api": "source.getSelectedRows", "value": [{"id": 7}]}]
        rows = [row for row in field_states(case, spec) if row["id"] == "source"]
        self.assertEqual({row["attribute"] for row in rows}, {"text", "selected_rows"})
        self.assertEqual(next(row for row in rows if row["attribute"] == "selected_rows")["candidate"]["reads"], 1)

    def test_indirect_action_is_the_default_focus_and_condition_is_navigable(self):
        before, after, report, _, _ = sample()
        options = flow_options(before, after, report)
        self.assertEqual(options[0]["node_key"], "assign")
        old, new = flow_lane(before, options[0]), flow_lane(after, options[0])
        self.assertEqual(old["cards"][1]["title"], "始终成立")
        self.assertEqual(new["cards"][1]["title"], "页面模式 = 新增")
        self.assertEqual(new["cards"][1]["target"]["node_key"], "if")
        self.assertEqual(new["cards"][3]["field"], "target")

    def test_same_label_source_swap_and_source_kind_change_retain_identity(self):
        before, after, _, _, _ = sample()
        target = {"group_key": "main", "node_key": "assign"}
        source = after["actionData"][0]["data"][1]["paramsValue"]["inputParams"]["attributeValue"]
        source["code"] = 'inbiz("other").value'
        old, new = flow_lane(before, target), flow_lane(after, target)
        self.assertEqual(old["cards"][0]["title"], new["cards"][0]["title"])
        self.assertNotEqual(old["cards"][0]["body"], new["cards"][0]["body"])
        self.assertFalse(same(old["cards"][0]["identity"], new["cards"][0]["identity"]))

    def test_condition_shortening_never_executes_or_guesses_arbitrary_expression(self):
        condition = {"target": {"code": 'inbiz.getPageMode() === "add" || dangerous()'}, "equalTo": "Equal", "value": True}
        self.assertIn("dangerous()", condition_text(condition))

    def test_trace_preserves_actual_order_and_value_without_inventing_dependencies(self):
        evidence = {"trace": [{"api": "source.getSelectedRows", "value": [{"id": 7}]},
                              {"api": "target.value.write", "value": 8}, {"api": "metadata.register", "version": 2}]}
        rows = trace_events(evidence)
        self.assertEqual([row["role"] for row in rows], ["读取选中行", "写入", "元数据"])
        self.assertEqual([row["index"] for row in rows], [0, 1, 2])
        self.assertEqual(rows[1]["body"], "8")

    def test_metadata_payload_and_return_are_observed_independently(self):
        _, _, _, case, spec = sample()
        case["expected"]["metadata"] = [{"code": "task", "value": "new"}]
        case["expected"]["last_return_undefined"] = True
        case["baseline"]["actual"].update(metadata=[{"code": "task", "value": "old"}], last_return=None)
        case["candidate"]["actual"].update(metadata=[{"code": "task", "value": "new"}], last_return_undefined=True)
        rows = {row["key"]: row for row in output_states(case, spec)}
        self.assertEqual(rows["metadata"]["baseline"]["verdict"], "failed")
        self.assertEqual(rows["metadata"]["candidate"]["verdict"], "passed")
        self.assertEqual(rows["last_return"]["baseline"]["verdict"], "failed")
        self.assertIs(rows["last_return"]["candidate"]["value"], UNDEFINED)

    def test_default_null_metadata_does_not_create_an_implied_payload(self):
        _, _, _, case, spec = sample()
        for version in ("baseline", "candidate"):
            case[version]["actual"].update(metadata=None, metadata_registrations=0, last_return_undefined=True)
        self.assertEqual(output_states(case, spec), [])


class FlowQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = application()

    def widget(self):
        before, after, report, _, _ = sample()
        original = deepcopy(report)
        widget = LogicReview()
        self.addCleanup(widget.close)
        widget.resize(1150, 850)
        widget.set_report(report, before=before, after=after)
        widget.show()
        self.app.processEvents()
        self.assertEqual(report, original)
        return widget

    def test_graph_is_default_and_card_text_survives_garbage_collection(self):
        widget = self.widget()
        gc.collect()
        self.assertEqual(widget.views.currentIndex(), 0)
        self.assertGreater(len(widget.flow.cards), 8)
        for card in widget.flow.cards:
            self.assertGreaterEqual(len(card.childItems()), 3)
            self.assertTrue(card.text_items[1].toPlainText())
        self.assertEqual(widget.flow.view.verticalScrollBar().value(), 0)

    def test_click_condition_card_opens_comparison_and_can_locate_controller(self):
        widget = self.widget()
        received = []
        widget.navigate_requested.connect(lambda *target: received.append(target))
        card = next(card for card in widget.flow.cards if card.target and card.target.get("node_key") == "if")
        widget.flow.view.ensureVisible(card)
        self.app.processEvents()
        center = widget.flow.view.mapFromScene(card.mapToScene(card.boundingRect().center()))
        QTest.mouseClick(widget.flow.view.viewport(), Qt.MouseButton.LeftButton, pos=center)
        self.app.processEvents()
        self.assertEqual(received, [])
        dialog = widget.flow.comparison_dialog
        self.addCleanup(dialog.close)
        self.assertTrue(dialog.isVisible())
        self.assertIn("执行条件", dialog.windowTitle())
        self.assertIn("inbiz.getPageMode()", widget.flow.comparison_views[1].toPlainText())
        widget.flow.comparison_locate_button.click()
        self.assertEqual(received, [("main", "if", "inputParams.condition")])

    def test_observed_values_are_on_destination_cards_and_track_expands(self):
        widget = self.widget()
        old_destination, new_destination = widget.flow.cards[3], widget.flow.cards[7]
        self.assertEqual(old_destination.color, "red")
        self.assertEqual(new_destination.color, "green")
        self.assertIn("EDIT-008 → A-001", old_destination.text_items[2].toPlainText())
        self.assertIn("保留原值", new_destination.text_items[2].toPlainText())
        widget.flow.trace_toggle.click()
        self.app.processEvents()
        self.assertTrue(any(card.text_items[0].toPlainText().endswith("写入") for card in widget.flow.cards))

    def test_small_window_keeps_both_versions_accessible(self):
        widget = self.widget()
        widget.resize(640, 640)
        self.app.processEvents()
        self.assertGreater(widget.flow.view.verticalScrollBar().maximum(), 0)
        self.assertGreaterEqual(widget.flow.view.transform().m11(), 0.55)
        self.assertEqual(len(widget.flow.options), 2)

    def test_small_changes_are_visible_inside_their_flow_block(self):
        widget = self.widget()
        rows = widget.flow.change_rows
        self.assertTrue(rows)
        self.assertTrue(any("inbiz.getPageMode()" in row["after"] for row in rows))
        self.assertFalse(widget.flow.change_cards, "Changes must not occupy a separate row above the graph")
        condition = widget.flow.cards[5]
        contents = "\n".join(item.toPlainText() for item in condition.inline_change_items)
        self.assertIn("inbiz.getPageMode()", contents)
        self.assertTrue(condition.details_button.isEnabled())
        self.assertTrue(condition.detail_payload["changes"])
        self.assertEqual(widget.flow.cards[0].inline_change_items, [])
        self.assertEqual(widget.flow.cards[0].detail_payload["changes"], [])
        self.assertFalse(widget.flow.cards[0].color in {"amber", "blue"}, "Unchanged source must not be marked changed")

    def test_long_change_is_bounded_on_graph_and_complete_in_comparison(self):
        widget = self.widget()
        value = 'inbiz("source").value + ' + '"full-value" + ' * 40 + '"END_OF_VALUE"'
        before, after, report, _, _ = sample()
        after["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["condition"]["Filters"][0]["target"]["code"] = value
        widget.set_report(report, before=before, after=after)
        condition = widget.flow.cards[5]
        visible = "\n".join(item.toPlainText() for item in condition.text_items)
        self.assertNotIn("END_OF_VALUE", visible)
        condition.details_button.click()
        self.app.processEvents()
        dialog = widget.flow.comparison_dialog
        self.addCleanup(dialog.close)
        self.assertTrue(dialog.isVisible())
        self.assertIn("END_OF_VALUE", widget.flow.comparison_views[1].toPlainText())
        self.assertIn("true", widget.flow.comparison_views[0].toPlainText())
        self.assertIn(value, widget.flow.comparison_raw_views[1].toPlainText().replace('\\"', '"'))

    def test_many_changes_are_collapsed_but_block_button_shows_all(self):
        widget = self.widget()
        before, after, report, _, _ = sample()
        filters = after["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["condition"]["Filters"]
        for index in range(7):
            filters.append({"target": {"paramTypes": "custom", "code": f"extra_filter_{index}"},
                            "equalTo": "Equal", "value": index})
        widget.set_report(report, before=before, after=after)
        condition = widget.flow.cards[5]
        changes = condition.detail_payload["changes"]
        self.assertGreaterEqual(len(changes), 7)
        self.assertLess(len(condition.inline_change_items), len(changes))
        visible = "\n".join(item.toPlainText() for item in condition.inline_change_items)
        self.assertNotIn("extra_filter_6", visible)
        condition.details_button.click()
        self.app.processEvents()
        self.addCleanup(widget.flow.comparison_dialog.close)
        complete = widget.flow.comparison_views[1].toPlainText()
        for index in range(7):
            self.assertIn(f"extra_filter_{index}", complete)
        self.assertIn("extra_filter_6", widget.flow.comparison_raw_views[1].toPlainText())

    def test_unchanged_block_opens_its_complete_values_without_other_block_changes(self):
        widget = self.widget()
        source = widget.flow.cards[0]
        self.assertEqual(source.detail_payload["changes"], [])
        source.details_button.click()
        self.app.processEvents()
        dialog = widget.flow.comparison_dialog
        self.addCleanup(dialog.close)
        self.assertTrue(dialog.isVisible())
        self.assertIn("数据来源", dialog.windowTitle())
        for view in widget.flow.comparison_full_views:
            contents = view.toPlainText()
            self.assertIn("来源编号", contents)
            self.assertNotIn("inbiz.getPageMode()", contents)
        self.assertTrue(all("未改动" in view.toPlainText() for view in widget.flow.comparison_views))
        self.assertTrue(all(view.toPlainText() for view in widget.flow.comparison_raw_views))
        self.assertNotIn("inbiz.getPageMode()", widget.flow.comparison_raw_views[1].toPlainText())

    def test_global_comparison_still_shows_all_changes(self):
        widget = self.widget()
        widget.flow.compare_button.click()
        self.app.processEvents()
        self.addCleanup(widget.flow.comparison_dialog.close)
        self.assertTrue(widget.flow.comparison_dialog.isVisible())
        self.assertIn("inbiz.getPageMode()", widget.flow.comparison_views[1].toPlainText())

    def test_markers_identify_each_native_controller_and_both_version_line_numbers(self):
        widget = self.widget()
        before, _, report, _, _ = sample()
        outer, assignment = before["actionData"][0]["data"]
        inner = deepcopy(outer)
        inner.update(key="if-inner", title="判断冻结资质", depth=["if"])
        assignment["depth"] = ["if", "if-inner"]
        before["actionData"][0]["data"] = [outer, inner, assignment]
        after = deepcopy(before)
        for index, code in enumerate(("page_is_add", "qualification_is_frozen")):
            after["actionData"][0]["data"][index]["paramsValue"]["inputParams"]["condition"]["Filters"][0]["target"]["code"] = code
        after["actionData"][0]["data"].insert(0, {
            "key": "prefix", "elementKey": "SetVariableValue", "title": "先读页面", "paramsValue": {}})
        widget.set_report(report, before=before, after=after,
                          action_context={"action_name": "审批动作", "action_code": "CodeA"})
        selected = next(index for index, option in enumerate(widget.flow.options) if option["node_key"] == "assign")
        widget.flow.flow_picker.setCurrentIndex(selected)
        self.app.processEvents()

        action = widget.flow.block_payloads[2]
        self.assertIn("审批动作（CodeA） · 初始化", action["marker"])
        self.assertIn("画布第 3 行 · 回填编号", action["baseline_marker"])
        self.assertIn("画布第 4 行 · 回填编号", action["candidate_marker"])
        self.assertEqual(widget.flow.cards[2].marker_item.toPlainText(), "第 3 行 · 回填编号")
        self.assertEqual(widget.flow.cards[6].marker_item.toPlainText(), "第 4 行 · 回填编号")

        widget.flow.cards[5].details_button.click()
        self.app.processEvents()
        self.addCleanup(widget.flow.comparison_dialog.close)
        for view, outer_line, inner_line in zip(widget.flow.comparison_views, (1, 2), (2, 3)):
            contents = view.toPlainText()
            self.assertIn("审批动作（CodeA） · 初始化", contents)
            self.assertIn(f"画布第 {outer_line} 行 · 判断新增", contents)
            self.assertIn(f"画布第 {inner_line} 行 · 判断冻结资质", contents)
            self.assertNotIn("回填编号", contents, "Controller changes must keep their own canvas markers")
        received = []
        widget.navigate_requested.connect(lambda *target: received.append(target))
        widget.flow.comparison_views[1].anchorClicked.emit(QUrl("gxp-marker:0"))
        self.assertEqual(received, [("main", "if", "inputParams.condition.Filters[0].target.code")])
        self.assertFalse(widget.flow.comparison_dialog.isVisible())

    def test_full_text_card_expands_instead_of_truncating_its_value(self):
        text = 'long-value ' * 80 + 'END_OF_VALUE'
        card = FlowCard("修改后", "完整参数", text, full_text=True)
        self.assertEqual(card.text_items[2].toPlainText(), text)
        self.assertGreater(card.card_height, 128)

    def test_difference_markup_escapes_code_and_highlights_the_changed_tokens(self):
        marked = difference_html('<old> & "same"', '<new> & "same"', "candidate")
        self.assertIn("background-color", marked)
        self.assertIn("new", marked)
        self.assertIn("&lt;", marked)
        self.assertNotIn("<new>", marked)

    def test_highlighted_change_preserves_spaces_tabs_and_line_breaks(self):
        value = '"a  b"\n\tEND_OF_VALUE'
        card = FlowCard("修改后", "完整表达式", value, full_text=True,
                        body_html=difference_html("old", value, "candidate"))
        self.assertEqual(card.text_items[2].toPlainText(), value)


if __name__ == "__main__":
    unittest.main()
