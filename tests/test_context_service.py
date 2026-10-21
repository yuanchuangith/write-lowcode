"""Read-only session evidence and saved-plan pagination without business IO."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import baseline, editor_config
from gxp_editor.approval import load_artifact, save_artifact
from gxp_editor.logic_review import LogicReviewError, build_business_review, plan_binding_hash
from gxp_editor.service import EditorService, EditorServiceError
from gxp_editor.state import StateStore


class ContextServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = StateStore(self.root / "state.sqlite3")
        self.owner = self.state.create_conversation("owner")["conversation_id"]
        self.other_owner = self.state.create_conversation("other")["conversation_id"]
        self.canvas = {"actionData": [{"key": "main", "title": "主动作", "actionType": "csharp", "data": [
            {"key": "query", "elementKey": "GetData", "title": "查询", "depth": [],
             "paramsValue": {"inputParams": {"status": "active", "deleted": 0, "kind": "old"},
                             "outputParams": {"result": "rows"}}}]}], "globalVariables": []}
        self.draft = baseline(data=json.dumps(self.canvas))
        self.draft["data_json"] = copy.deepcopy(self.canvas)
        self.binding = {"compiler_binding_sha256": "a" * 64}
        self.session = self.state.create_session(self.draft, "backup-context", self.binding["compiler_binding_sha256"],
                                                 conversation_id=self.owner)
        self.registry = {"elements": [{"key": "GetData", "name": "查询", "input_params": [], "output_params": []}]}
        self.repository = Mock()
        self.repository.load_draft_by_id.return_value = copy.deepcopy(self.draft)
        self.compiler = Mock()
        self.compiler.describe_registry.return_value = self.registry
        self.compiler.compiler_binding.return_value = self.binding
        self.service = self.service_for(self.owner)
        self.components = Mock(return_value=(editor_config(), object(), self.repository, self.compiler, object()))
        self.enterContext(patch.object(self.service, "_components", self.components))
        self.targets = [{"group_key": "main", "node_key": "query"}]

    def service_for(self, owner):
        service = object.__new__(EditorService)
        service.state = self.state
        service._conversation_id = owner
        return service

    def saved_plan(self):
        desired_canvas = copy.deepcopy(self.canvas)
        desired_canvas["actionData"][0]["data"][0]["paramsValue"]["inputParams"].update(
            status="freeze", deleted=1, kind="new")
        candidate = {"data": json.dumps(desired_canvas), "js_code": "", "csharp_code": "candidate", "relation_data": {}}
        spec = {"schema_version": 1, "revision": "request-1", "requirements": [
            {"id": "query", "text": "更新查询", "source_text": "用户：更新查询配置", "targets": self.targets}],
             "scenarios": {"schema_version": 1, "cases": []}}
        report = build_business_review(review_spec=spec, baseline_design=self.draft,
                                       candidate_design=candidate, compiler_binding=self.binding)
        artifact = {"base_content_hash": self.draft["content_hash"], "compiler_binding": self.binding,
                    "desired": candidate, "business_review": report}
        path = self.root / "plan.json"
        save_artifact(path, artifact)
        plan_id = self.state.create_plan(self.session["id"], str(path), self.draft["content_hash"],
            plan_binding_hash(artifact), self.binding["compiler_binding_sha256"],
            conversation_id=self.owner, owner_revision=0)
        return plan_id, report, path

    def forbid_writes(self):
        stack = ExitStack()
        for name in ("create_plan", "create_transaction", "update_session", "update_plan", "update_transaction"):
            if hasattr(self.state, name):
                stack.enter_context(patch.object(self.state, name, side_effect=AssertionError("Unexpected state write: " + name)))
        stack.enter_context(patch("gxp_editor.service.save_artifact", side_effect=AssertionError("Unexpected artifact write")))
        stack.enter_context(patch.object(self.service, "_launch_approval", side_effect=AssertionError("Unexpected approval launch")))
        return stack

    def test_context_is_bound_to_owned_baseline_backup_and_compiler_without_writing(self):
        before = self.state.get_session(self.session["id"])
        with self.forbid_writes():
            result = self.service.inspect_edit_context(self.session["id"], self.targets)
        self.assertEqual(result["session_id"], self.session["id"])
        self.assertEqual(result["backup_id"], "backup-context")
        self.assertEqual(result["design_id"], self.draft["id"])
        self.assertEqual(result["baseline_content_hash"], self.draft["content_hash"])
        self.assertEqual(result["compiler_binding"], self.binding)
        self.assertFalse(result["write_executed"])
        self.assertTrue(result["read_only"])
        self.assertEqual(result["nodes"][0]["params_value"], self.canvas["actionData"][0]["data"][0]["paramsValue"])
        self.assertEqual(self.state.get_session(self.session["id"]), before)
        self.assertEqual(self.state.session_transactions(self.session["id"]), [])
        self.compiler.compile_patch.assert_not_called()
        self.repository.load_draft_by_id.assert_called_once_with(self.draft["id"])

    def test_cross_conversation_context_is_rejected_before_component_loading(self):
        self.service._conversation_id = self.other_owner
        with self.assertRaisesRegex(EditorServiceError, "其他对话"):
            self.service.inspect_edit_context(self.session["id"], self.targets)
        self.components.assert_not_called()

    def test_changed_draft_rejects_stale_context_before_registry_read(self):
        self.repository.load_draft_by_id.return_value["content_hash"] = "b" * 64
        with self.assertRaisesRegex(EditorServiceError, "stale"):
            self.service.inspect_edit_context(self.session["id"], self.targets)
        self.compiler.describe_registry.assert_not_called()

    def test_changed_compiler_binding_rejects_stale_native_contract(self):
        self.compiler.compiler_binding.return_value = {"compiler_binding_sha256": "b" * 64}
        with self.assertRaisesRegex(EditorServiceError, "contracts are stale"):
            self.service.inspect_edit_context(self.session["id"], self.targets)

    def test_transfer_during_contract_loading_does_not_return_evidence_to_old_owner(self):
        def transfer():
            self.state.transfer_session(self.session["id"], self.other_owner, expected_owner=self.owner, expected_revision=0)
            return self.registry
        self.compiler.describe_registry.side_effect = transfer
        with self.assertRaisesRegex(EditorServiceError, "其他对话"):
            self.service.inspect_edit_context(self.session["id"], self.targets)

    def test_round_trip_transfer_during_context_loading_rejects_stale_owner_revision(self):
        def transfer():
            self.state.transfer_session(self.session["id"], self.other_owner, expected_owner=self.owner, expected_revision=0)
            self.state.transfer_session(self.session["id"], self.owner, expected_owner=self.other_owner, expected_revision=1)
            return self.registry
        self.compiler.describe_registry.side_effect = transfer
        with self.assertRaisesRegex(EditorServiceError, "ownership changed"):
            self.service.inspect_edit_context(self.session["id"], self.targets)

    def test_finished_session_does_not_load_context(self):
        self.state.update_session(self.session["id"], "finished", conversation_id=self.owner, owner_revision=0)
        with self.assertRaisesRegex(EditorServiceError, "active edit session"):
            self.service.inspect_edit_context(self.session["id"], self.targets)
        self.components.assert_not_called()

    def test_saved_report_pagination_and_id_lookup_are_read_only(self):
        plan_id, report, path = self.saved_plan()
        original = path.read_bytes()
        session_before = self.state.get_session(self.session["id"])
        plan_before = self.state.get_plan(plan_id)
        with self.forbid_writes():
            page = self.service.read_plan_review(plan_id, section="changes", offset=1, limit=1)
            last = self.service.read_plan_review(plan_id, section="changes", offset=2, limit=1)
            requirement = self.service.read_plan_review(plan_id, section="requirements", item_id="query")
            summary = self.service.read_plan_review(plan_id)
        self.assertEqual(page["items"], report["changes"][1:2])
        self.assertEqual(page["next_offset"], 2)
        self.assertIsNone(last["next_offset"])
        self.assertEqual(page["report_sha256"], report["review_sha256"])
        self.assertEqual(requirement["items"], report["requirements"])
        self.assertEqual(summary["summary"], report["summary"])
        self.assertTrue(page["current_plan"])
        self.assertTrue(page["read_only"])
        self.assertFalse(page["write_executed"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.state.get_session(self.session["id"]), session_before)
        self.assertEqual(self.state.get_plan(plan_id), plan_before)
        self.assertEqual(self.state.session_transactions(self.session["id"]), [])
        self.components.assert_not_called()

    def test_cross_conversation_report_is_rejected_before_artifact_read(self):
        plan_id, _, _ = self.saved_plan()
        self.service._conversation_id = self.other_owner
        with patch("gxp_editor.service.load_artifact") as read:
            with self.assertRaisesRegex(EditorServiceError, "其他对话"):
                self.service.read_plan_review(plan_id)
        read.assert_not_called()

    def test_tampered_saved_report_or_candidate_cannot_be_paginated(self):
        plan_id, _, path = self.saved_plan()
        original = load_artifact(path)
        for mutate in (lambda a: a["business_review"].update(status="passed"),
                       lambda a: a["desired"].update(csharp_code="tampered"),
                       lambda a: a["business_review"]["changes"].clear()):
            with self.subTest(mutate=mutate):
                artifact = copy.deepcopy(original)
                mutate(artifact)
                save_artifact(path, artifact)
                with self.assertRaises((EditorServiceError, LogicReviewError)):
                    self.service.read_plan_review(plan_id, section="changes")

    def test_report_transfer_during_artifact_loading_does_not_return_evidence_to_old_owner(self):
        plan_id, _, _ = self.saved_plan()
        def transfer(path):
            self.state.transfer_session(self.session["id"], self.other_owner, expected_owner=self.owner, expected_revision=0)
            return load_artifact(path)
        with patch("gxp_editor.service.load_artifact", side_effect=transfer):
            with self.assertRaisesRegex(EditorServiceError, "其他对话|ownership changed"):
                self.service.read_plan_review(plan_id)

    def test_report_round_trip_transfer_invalidates_initial_owner_revision(self):
        plan_id, _, _ = self.saved_plan()
        def transfer(path):
            self.state.transfer_session(self.session["id"], self.other_owner, expected_owner=self.owner, expected_revision=0)
            self.state.transfer_session(self.session["id"], self.owner, expected_owner=self.other_owner, expected_revision=1)
            return load_artifact(path)
        with patch("gxp_editor.service.load_artifact", side_effect=transfer):
            with self.assertRaisesRegex(EditorServiceError, "ownership changed|归属"):
                self.service.read_plan_review(plan_id)

    def test_invalid_pagination_and_unknown_section_are_rejected(self):
        plan_id, _, _ = self.saved_plan()
        for options in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 21}, {"section": "unknown"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.service.read_plan_review(plan_id, **options)
