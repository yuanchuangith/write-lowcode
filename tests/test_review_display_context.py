"""Shared display derivations retain full evidence and are rebuilt per report."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from gxp_editor import component_preview, logic_flow_model, logic_review_model
from gxp_editor.hashing import sha256_json
from gxp_editor.logic_flow_ui import LogicFlowReview
from gxp_editor.logic_review_ui import LogicReview
from gxp_editor.qt_theme import application
from tests.test_logic_flow import sample


class DisplayContextTests(unittest.TestCase):
    def test_shared_and_standalone_models_produce_identical_evidence(self):
        before, after, report, _, _ = sample()
        original = deepcopy((before, after, report))
        signature = sha256_json(report)
        context = logic_review_model.review_display_context(before, after)
        self.assertEqual(context["changes"], component_preview.component_changes(before, after))
        self.assertEqual(context["impacts"], component_preview.control_flow_impacts(before, after))
        options = logic_flow_model.flow_options(before, after, report)
        self.assertEqual(options, logic_flow_model.flow_options(before, after, report, display_context=context))
        self.assertEqual(logic_flow_model.field_names(before, after),
                         logic_flow_model.field_names(before, after, display_context=context))
        for target in options:
            self.assertEqual(logic_flow_model.flow_comparison(before, after, target),
                             logic_flow_model.flow_comparison(before, after, target, display_context=context))
        for change in report["changes"]:
            self.assertEqual(logic_review_model.change_display_context(change, before, after),
                             logic_review_model.change_display_context(change, before, after, context=context))
        self.assertEqual((before, after, report), original)
        self.assertEqual(sha256_json(report), signature)

    def test_shared_models_do_not_rescan_canvas_or_recompute_impacts(self):
        before, after, report, _, _ = sample()
        context = logic_review_model.review_display_context(before, after)
        with patch.object(logic_flow_model, "canvas_node_locations", side_effect=AssertionError("rescan")), \
                patch.object(logic_flow_model, "control_flow_impacts", side_effect=AssertionError("recompute")):
            options = logic_flow_model.flow_options(before, after, report, display_context=context)
            logic_flow_model.field_names(before, after, display_context=context)
            for target in options:
                logic_flow_model.flow_comparison(before, after, target, display_context=context)

    def test_no_control_change_skips_dependency_and_location_scan(self):
        before, after, _, _, _ = sample()
        after = deepcopy(before)
        after["actionData"][0]["data"][1]["paramsValue"]["inputParams"]["attributeValue"]["code"] = "replacement"
        changes = component_preview.component_changes(before, after)
        with patch.object(component_preview, "canvas_node_locations", side_effect=AssertionError("rescan")):
            self.assertEqual(component_preview.control_flow_impacts(before, after, changes=changes), [])


class DisplayContextWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = application()

    def test_full_review_derives_each_shared_input_once(self):
        before, after, report, _, _ = sample()
        original = deepcopy((before, after, report))
        widget = LogicReview()
        self.addCleanup(widget.close)
        locations = component_preview.canvas_node_locations
        changes = component_preview.component_changes
        impacts = component_preview.control_flow_impacts
        with patch.object(logic_review_model, "canvas_node_locations", wraps=locations) as review_locations, \
                patch.object(component_preview, "canvas_node_locations", wraps=locations) as impact_locations, \
                patch.object(logic_flow_model, "canvas_node_locations", wraps=locations) as flow_locations, \
                patch.object(logic_review_model, "component_changes", wraps=changes) as review_changes, \
                patch.object(component_preview, "component_changes", wraps=changes) as impact_changes, \
                patch.object(logic_review_model, "control_flow_impacts", wraps=impacts) as review_impacts:
            widget.set_report(report, before=before, after=after)
            self.assertEqual(review_locations.call_count + impact_locations.call_count + flow_locations.call_count, 2)
            self.assertEqual(review_changes.call_count + impact_changes.call_count, 1)
            self.assertEqual(review_impacts.call_count, 1)
        self.assertEqual((before, after, report), original)

    def test_new_report_rebuilds_context_instead_of_reusing_previous_locations(self):
        before, after, report, _, _ = sample()
        widget = LogicFlowReview()
        self.addCleanup(widget.close)
        widget.set_report(report, before=before, after=after)
        previous = widget.display_context
        next_canvas = deepcopy(after)
        next_canvas["actionData"][0]["data"].insert(0, {"key": "first", "elementKey": "Breakpoint"})
        widget.set_report(report, before=after, after=next_canvas)
        self.assertIsNot(widget.display_context, previous)
        self.assertEqual(widget.node_locations["candidate"][("main", "assign")]["line"], 3)
        self.assertEqual(previous["after_locations"][("main", "assign")]["line"], 2)


if __name__ == "__main__":
    unittest.main()
