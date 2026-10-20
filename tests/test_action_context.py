"""Resolve/display the actual edit target without guessing a form by its name."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

from PySide6.QtWidgets import QTabWidget

from tests.helpers import baseline, transaction
from tests.test_logic_flow import sample
from gxp_editor.action_context import action_context, context_details
from gxp_editor.action_context_ui import ActionContextBanner
from gxp_editor.approval import transaction_binding_hash
from gxp_editor.logic_review_ui import LogicReview
from gxp_editor.qt_review_ui import ComponentReview
from gxp_editor.qt_theme import application
from gxp_editor.repository import EditorRepository
from gxp_editor.transaction import TransactionPlanError, validate_transaction_artifact


def form_baseline():
    return baseline(metadata={"action_type": "form", "action_name": "初始化申请", "action_code": "ActionOne",
        "page_id": "training/request", "form_name": "岗位培训申请", "page_route": "training/request",
        "page_record_id": "page-1", "application_id": "app-1", "page_resolution": "resolved"})


class ActionContextTests(unittest.TestCase):
    def test_snapshot_identifies_form_action_and_exact_target(self):
        context = action_context(form_baseline())
        self.assertEqual(context["form_name"], "岗位培训申请")
        self.assertEqual((context["ref_id"], context["design_id"]), ("ref-1", "design-1"))
        self.assertIn("training/request", context_details(context))
        self.assertIn("ActionOne", context_details(context))

    def test_public_action_never_claims_a_single_owner_form(self):
        draft = form_baseline()
        draft["metadata"]["action_type"] = "public"
        context = action_context(draft)
        self.assertEqual(context["form_name"], "")
        self.assertEqual(context["page_id"], "")
        self.assertIn("多个表单", context_details(context))

    def test_legacy_action_does_not_invent_a_form(self):
        context = action_context(baseline())
        self.assertEqual(context["form_name"], "")
        self.assertIn("待确认", context_details(context))

    def test_page_lookup_is_exact_and_parameterized(self):
        session = Mock()
        identifier = "x' OR 1=1 --"
        session.query.return_value = [{"page_record_id": "p-1", "form_name": "表单甲", "page_route": identifier}]
        context = EditorRepository._page_context(identifier, session)
        sql, params = session.query.call_args.args
        self.assertNotIn(identifier, sql)
        self.assertEqual(params["page_id"], identifier)
        self.assertEqual(context["form_name"], "表单甲")

    def test_ambiguous_missing_and_denied_catalog_never_choose_a_form(self):
        for rows, status in (([], "missing"), ([{"form_name": "甲"}, {"form_name": "乙"}], "ambiguous")):
            session = Mock()
            session.query.return_value = rows
            result = EditorRepository._page_context("page", session)
            self.assertEqual(result["page_resolution"], status)
            self.assertNotIn("form_name", result)
        session.query.side_effect = RuntimeError("private connection details")
        self.assertEqual(EditorRepository._page_context("page", session), {"page_resolution": "unavailable"})

    def test_unresolved_translation_key_is_not_presented_as_a_form_name(self):
        session = Mock()
        session.query.return_value = [{"form_name": "{multilingual}global.Form1"}]
        self.assertEqual(EditorRepository._page_context("page", session)["form_name"], "")

    def test_resolve_action_enriches_only_form_catalog_identity(self):
        repository = EditorRepository(Mock())
        session = Mock()
        def query(sql, params, **kwargs):
            if "FROM inbiz_page page" in sql:
                return [{"page_record_id": "page-1", "form_name": "表单甲", "page_route": "route-1"}]
            if "FROM `cpm_bizflows`" in sql:
                return [{"ref_id": "ref-1", "action_type": "form", "page_id": "route-1", "action_code": "ACT", "action_name": "初始化"}]
            return []
        session.query.side_effect = query
        metadata = repository.resolve_action("ACT", session)
        self.assertEqual(metadata["form_name"], "表单甲")
        self.assertEqual(metadata["page_id"], "route-1")

    def test_new_identity_is_bound_and_old_artifacts_remain_readable(self):
        artifact = transaction()
        artifact["action_context"]["form_name"] = "其他表单"
        with self.assertRaisesRegex(TransactionPlanError, "metadata hash"):
            validate_transaction_artifact(artifact)
        del artifact["action_context"]
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        validate_transaction_artifact(artifact)

    def test_rebinding_context_to_another_target_is_rejected(self):
        artifact = transaction()
        artifact["action_context"]["design_id"] = "another-design"
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "context"):
            validate_transaction_artifact(artifact)


class ContextQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = application()

    def test_identity_visible_and_copy_includes_exact_action_and_page(self):
        context = action_context(form_baseline())
        original = deepcopy(context)
        banner = ActionContextBanner(context)
        self.addCleanup(banner.close)
        banner.set_group("main", "页面初始化")
        self.assertEqual(banner.form_label.text(), "岗位培训申请")
        self.assertEqual(banner.action_label.text(), "初始化申请")
        banner.copy_button.click()
        text = self.app.clipboard().text()
        for expected in ("岗位培训申请", "training/request", "ActionOne", "design-1", "main"):
            self.assertIn(expected, text)
        self.assertEqual(context, original)

    def test_group_tracks_flow_canvas_and_tab_switch(self):
        before, after, report, _, _ = sample()
        after["actionData"].append({"key": "other", "title": "保存按钮", "data": [
            {"key": "other-node", "title": "其他节点", "elementKey": "SetVariable", "paramsValue": {}}]})
        notebook = QTabWidget()
        self.addCleanup(notebook.close)
        logic, components = LogicReview(), ComponentReview()
        notebook.addTab(logic, "验收")
        notebook.addTab(components, "画布")
        banner = ActionContextBanner(action_context(form_baseline()))
        self.addCleanup(banner.close)
        banner.bind_reviews(notebook, logic, components)
        components.render(before, after)
        logic.set_report(report, before=before, after=after)
        self.assertEqual(banner.group_label.text(), "初始化")
        notebook.setCurrentWidget(components)
        components.select_canvas_node("other", "other-node")
        self.assertEqual(banner.group_label.text(), "保存按钮")
        notebook.setCurrentWidget(logic)
        self.assertEqual(banner.group_label.text(), "初始化")


if __name__ == "__main__":
    unittest.main()
