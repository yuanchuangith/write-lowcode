"""Readonly records are hints checked before any session occupancy is acquired."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.helpers import baseline, editor_config
from gxp_editor.handoff_store import (HandoffStore, HandoffError, environment_scope,
                                      snapshot_targets, compare_targets, design_content_sha256)
from gxp_editor.handoff_import import inspect_handoff
from gxp_editor.hashing import sha256_text
from gxp_editor.service import EditorService


class HandoffImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name}))
        self.data = {"actionData": [{"key": "main", "title": "Main", "actionType": "js", "data": [
            {"key": "n", "elementKey": "Noop", "title": "Target", "depth": [],
             "paramsValue": {"inputParams": {"value": "old"}, "outputParams": {}}}]}], "globalVariables": []}
        self.draft = baseline(data=json.dumps(self.data), data_json=self.data)
        self.config = editor_config()
        self.targets = [{"group_key": "main", "node_key": "n"}]
        self.record = {"environment_scope": environment_scope(self.config.database),
            "action": {"ref_id": self.draft["ref_id"], "action_code": self.draft["code"], "action_name": "Test"},
            "draft": {"design_id": self.draft["id"], "data_sha256": sha256_text(self.draft["data"]),
                      "semantic_sha256": design_content_sha256(self.draft["data"])},
            "diagnosis_source": {"design_id": self.draft["id"], "version": "draft", "data_sha256": sha256_text(self.draft["data"])},
            "targets": snapshot_targets(self.data, self.targets), "diagnosis_targets": snapshot_targets(self.data, self.targets),
            "source_target_differences": [], "change_spec": {"user_request": "Change value", "summary": "Set value to new",
                "changes": [{**self.targets[0], "description": "Change value", "parameter_path": "/inputParams/value", "before": "old", "after": "new"}],
                "preserve": ["Keep the callback signature"], "scenarios": ["Existing output is preserved"]}}
        self.diagnosis = {"issues": [{"issue_id": "BUG-01", "status": "confirmed",
            "symptom": "The target still uses the old value", "expected": "Use the new value",
            "root_cause": "The target parameter remains configured as old",
            "solution": "Change only the target input parameter",
            "evidence": [{"source": "inspect_action", "finding": "The complete target parameter is old",
                "group_key": "main", "node_key": "n", "design_id": self.draft["id"]}],
            "verification": "static_confirmed"}],
            "excluded": ["The callback signature is not the cause"],
            "unresolved": ["Runtime acceptance remains pending"]}

    def export(self):
        return HandoffStore().write(self.record)["handoff_ref"]

    def service(self):
        service = object.__new__(EditorService)
        service._conversation_id = "new-dialogue"
        service.state = Mock()
        service.state.active_session.return_value = None
        service.state.target_session.return_value = None
        repository, compiler = Mock(), Mock()
        repository.load_draft_by_identifier.return_value = self.draft
        service._components = Mock(return_value=(self.config, Mock(), repository, compiler, Mock()))
        return service, compiler

    def start(self, service, compiler, **kwargs):
        compiler.describe_registry.return_value = {"elements": [{"key": "Noop"}], "bridge_sha256": "a" * 64}
        compiler.compiler_binding.return_value = {"compiler_binding_sha256": "b" * 64}
        compiler.validate_design.return_value = {"validation": {"valid": True}}
        service.state.create_session.return_value = {"id": "independent-session", "ref_id": self.draft["ref_id"],
            "design_id": self.draft["id"], "action_code": self.draft["code"], "action_name": "Test"}
        with patch("gxp_editor.service.approval_secret", return_value="test-secret"), \
                patch("gxp_editor.service.BackupStore") as store, \
                patch.object(service, "editor_status", return_value={"planning_ready": True, "write_ready": True,
                    "production_ready": False, "gates": {}}):
            store.return_value.create.return_value = {"backup_id": "test", "files": {}}
            return service.begin_edit_session("ActionOne", **kwargs)

    def test_matching_record_has_no_inherited_authority_and_no_generated_code(self):
        result = inspect_handoff(self.export(), self.draft, self.config.database)
        self.assertEqual(result["status"], "matched")
        self.assertFalse(result["authorization_inherited"])
        self.assertFalse(result["approval_inherited"])
        self.assertEqual(result["change_spec"]["preserve"], ["Keep the callback signature"])
        self.assertNotIn("js_code", result)

    def test_evidence_only_record_does_not_require_a_cause_or_patch(self):
        issue = copy.deepcopy(self.diagnosis["issues"][0])
        for key in ("expected", "root_cause", "solution"):
            issue.pop(key)
        issue.update(status="candidate", verification="pending")
        self.record["change_spec"] = {"user_request": "Investigate the target value",
            "summary": "Saved target evidence; cause and solution are not yet known",
            "diagnosis": {"issues": [issue], "unresolved": ["Determine the cause and expected behavior"]}}
        result = inspect_handoff(self.export(), self.draft, self.config.database)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["change_spec"], self.record["change_spec"])
        self.assertNotIn("changes", result["change_spec"])
        self.assertFalse(result["authorization_inherited"])

    def test_diagnosis_without_draft_snapshot_starts_normally_without_reused_target_context(self):
        self.record.update(record_kind="diagnosis_only", draft=None, targets=[],
                           diagnosis_targets=[], source_target_differences=[])
        self.record["change_spec"] = {"user_request": "Investigate this action",
            "summary": "The symptom is known but the current target is not yet located",
            "diagnosis": {"issues": [{"issue_id": "early", "status": "candidate",
                "symptom": "Unexpected value", "evidence": [{"source": "reported symptom",
                    "finding": "An unexpected value was observed"}], "verification": "pending"}],
                "unresolved": ["Locate the affected node and establish the cause"]}}
        reference = self.export()
        service, compiler = self.service()
        result = self.start(service, compiler, handoff_ref=reference)
        self.assertEqual(result["session_id"], "independent-session")
        self.assertEqual(result["handoff"]["status"], "diagnosis_only")
        self.assertFalse(result["handoff"]["snapshot_reused"])
        self.assertTrue(result["handoff"]["requires_current_target_inspection"])
        self.assertNotIn("edit_context", result)
        self.assertEqual(result["handoff"]["change_spec"], self.record["change_spec"])
        self.assertFalse(result["handoff"]["approval_inherited"])

    def test_editor_can_start_normally_without_saved_evidence(self):
        service, compiler = self.service()
        compiler.describe_registry.return_value = {"elements": [{"key": "Noop"}], "bridge_sha256": "a" * 64}
        compiler.compiler_binding.return_value = {"compiler_binding_sha256": "b" * 64}
        compiler.validate_design.return_value = {"validation": {"valid": True}}
        service.state.create_session.return_value = {"id": "independent-session", "ref_id": self.draft["ref_id"],
            "design_id": self.draft["id"], "action_code": self.draft["code"], "action_name": "Test"}
        with patch("gxp_editor.service.approval_secret", return_value="test-secret"), \
                patch("gxp_editor.service.BackupStore") as store, \
                patch.object(service, "editor_status", return_value={"planning_ready": True, "write_ready": True,
                    "production_ready": False, "gates": {}}), \
                patch("gxp_editor.handoff_import.inspect_handoff") as inspect_saved:
            store.return_value.create.return_value = {"backup_id": "test", "files": {}}
            result = service.begin_edit_session("ActionOne")
        self.assertEqual(result["session_id"], "independent-session")
        self.assertNotIn("handoff", result)
        self.assertEqual(result["evidence_candidates"]["count"], 0)
        inspect_saved.assert_not_called()
        compiler.compile_patch.assert_not_called()

    def test_normal_start_discovers_only_same_environment_and_exact_action_without_selection(self):
        reference = self.export()
        other_environment = copy.deepcopy(self.record)
        other_environment["environment_scope"] = "f" * 64
        HandoffStore().write(other_environment)
        other_action = copy.deepcopy(self.record)
        other_action["action"]["ref_id"] = "different-ref"
        other_action["action"]["action_code"] = self.draft["ref_id"]
        HandoffStore().write(other_action)
        service, compiler = self.service()
        with patch("gxp_editor.handoff_import.inspect_handoff") as importer:
            result = self.start(service, compiler)
        hints = result["evidence_candidates"]
        self.assertEqual(hints["status"], "available")
        self.assertEqual(hints["count"], 1)
        self.assertEqual(hints["candidates"][0]["handoff_id"], reference["handoff_id"])
        self.assertTrue(hints["selection_required"])
        self.assertNotIn("handoff", result)
        self.assertNotIn("edit_context", result)
        self.assertFalse(hints["authorization_inherited"])
        importer.assert_not_called()

    def test_discovery_failure_never_blocks_normal_start(self):
        service, compiler = self.service()
        with patch.object(HandoffStore, "list_page", side_effect=OSError("private-path-and-data")):
            result = self.start(service, compiler)
        self.assertEqual(result["session_id"], "independent-session")
        self.assertEqual(result["evidence_candidates"]["status"], "unavailable")
        self.assertNotIn("private-path-and-data", json.dumps(result))
        service.state.create_session.assert_called_once()

    def test_reused_session_also_returns_optional_evidence_candidates(self):
        reference = self.export()
        service, compiler = self.service()
        compiler.compiler_binding.return_value = {"compiler_binding_sha256": "b" * 64}
        session = {"id": "existing", "conversation_id": "new-dialogue", "state": "locked",
            "ref_id": self.draft["ref_id"], "design_id": self.draft["id"], "action_code": self.draft["code"],
            "action_name": "Test", "backup_id": "old-backup", "compiler_hash": "b" * 64,
            "baseline": {**self.draft, "relations": []}}
        service.state.active_session.return_value = session
        service.state.target_session.return_value = session
        service._owned_session = Mock(return_value=session)
        service._components.return_value[2].load_relations.return_value = []
        result = service.begin_edit_session("ActionOne")
        self.assertTrue(result["session_reused"])
        self.assertFalse(result["approval_granted"])
        self.assertEqual(result["evidence_candidates"]["candidates"][0]["handoff_id"], reference["handoff_id"])
        service.state.create_session.assert_not_called()

    def test_discovery_is_bounded_and_returns_optional_next_page(self):
        for _ in range(6):
            self.export()
        service, compiler = self.service()
        result = self.start(service, compiler)
        hints = result["evidence_candidates"]
        self.assertEqual(hints["count"], 5)
        self.assertTrue(hints["has_more"])
        self.assertEqual(hints["next_read"]["arguments"]["cursor"], hints["next_cursor"])
        self.assertEqual(hints["next_read"]["arguments"]["environment_scope"], environment_scope(self.config.database))

    def test_matching_record_starts_this_dialogue_with_target_context(self):
        self.record["change_spec"]["diagnosis"] = copy.deepcopy(self.diagnosis)
        service, compiler = self.service()
        compiler.describe_registry.return_value = {"elements": [{"key": "Noop"}], "bridge_sha256": "a" * 64}
        compiler.compiler_binding.return_value = {"compiler_binding_sha256": "b" * 64}
        compiler.validate_design.return_value = {"validation": {"valid": True}}
        service.state.create_session.return_value = {"id": "new-session", "ref_id": self.draft["ref_id"],
            "design_id": self.draft["id"], "action_code": self.draft["code"], "action_name": "Test"}
        with patch("gxp_editor.service.approval_secret", return_value="test-secret"), \
                patch("gxp_editor.service.BackupStore") as store, \
                patch.object(service, "editor_status", return_value={"planning_ready": True, "write_ready": True,
                    "production_ready": False, "gates": {}}):
            store.return_value.create.return_value = {"backup_id": "test", "files": {}}
            result = service.begin_edit_session("ActionOne", handoff_ref=self.export())
        self.assertEqual(result["conversation_id"], "new-dialogue")
        self.assertEqual(result["session_id"], "new-session")
        self.assertEqual(result["edit_context"]["nodes"][0]["params_value"], self.data["actionData"][0]["data"][0]["paramsValue"])
        self.assertFalse(result["handoff"]["approval_inherited"])
        self.assertEqual(result["handoff"]["change_spec"]["diagnosis"], self.diagnosis)
        self.assertEqual(result["handoff"]["change_spec"]["changes"], self.record["change_spec"]["changes"])
        self.assertEqual(result["handoff"]["change_spec"]["scenarios"], self.record["change_spec"]["scenarios"])
        compiler.compile_patch.assert_not_called()
        self.assertEqual(service.state.create_session.call_args.kwargs["conversation_id"], "new-dialogue")

    def test_stale_draft_reports_only_changed_target_locations(self):
        self.record["change_spec"]["diagnosis"] = copy.deepcopy(self.diagnosis)
        reference = self.export()
        changed = copy.deepcopy(self.draft)
        changed["data_json"]["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["value"] = "other"
        changed["data"] = json.dumps(changed["data_json"])
        result = inspect_handoff(reference, changed, self.config.database)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["stale_reason"], "target_content_changed")
        self.assertEqual(result["target_differences"][0]["node_key"], "n")
        self.assertIn("paramsValue", result["target_differences"][0]["changed_fields"])
        self.assertEqual(result["change_spec"]["diagnosis"], self.diagnosis)
        self.assertEqual(result["change_spec"]["preserve"], self.record["change_spec"]["preserve"])
        self.assertFalse(result["authorization_inherited"])

    def test_formatting_only_draft_change_reuses_semantically_identical_snapshot(self):
        reference = self.export()
        formatted = {**self.draft, "data": json.dumps(self.data, indent=2)}
        result = inspect_handoff(reference, formatted, self.config.database)
        self.assertEqual(result["status"], "matched")
        self.assertTrue(result["freshness"]["formatting_only"])
        self.assertFalse(result["freshness"]["semantic_content_changed"])
        self.assertTrue(result["freshness"]["target_snapshot_current"])

    def test_other_group_change_is_still_stale_with_precise_refresh_scope(self):
        reference = self.export()
        changed = copy.deepcopy(self.draft)
        changed["data_json"]["actionData"].append({"key": "other", "data": []})
        changed["data"] = json.dumps(changed["data_json"])
        result = inspect_handoff(reference, changed, self.config.database)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["stale_reason"], "other_draft_content_changed")
        self.assertEqual(result["target_differences"], [])
        self.assertEqual(result["refresh_scope"]["targets"], [])
        self.assertTrue(result["freshness"]["semantic_content_changed"])

    def test_group_context_change_is_not_mistaken_for_target_parameter_change(self):
        reference = self.export()
        changed = copy.deepcopy(self.draft)
        changed["data_json"]["actionData"][0]["inputParams"] = {"new_dependency": "value"}
        changed["data"] = json.dumps(changed["data_json"])
        result = inspect_handoff(reference, changed, self.config.database)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["stale_reason"], "target_context_changed")
        self.assertEqual(result["target_differences"][0]["changed_fields"], ["control_flow"])

    def test_legacy_record_without_semantic_hash_remains_conservatively_stale(self):
        self.record["draft"].pop("semantic_sha256")
        reference = self.export()
        result = inspect_handoff(reference, {**self.draft, "data": json.dumps(self.data, indent=2)}, self.config.database)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["stale_reason"], "legacy_content_changed")
        self.assertIsNone(result["freshness"]["semantic_content_changed"])

    def test_raw_draft_json_is_authoritative_over_cached_parsed_data(self):
        reference = self.export()
        raw_changed = copy.deepcopy(self.data)
        raw_changed["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["value"] = "changed"
        result = inspect_handoff(reference, {**self.draft, "data": json.dumps(raw_changed)}, self.config.database)
        self.assertEqual(result["stale_reason"], "target_content_changed")

    def test_different_environment_or_action_is_rejected(self):
        reference = self.export()
        for change in ({"ref_id": "other"}, {"id": "other"}, {"code": "other"}):
            with self.subTest(change=change), self.assertRaises(HandoffError):
                inspect_handoff(reference, {**self.draft, **change}, self.config.database)
        with self.assertRaises(HandoffError):
            inspect_handoff(reference, self.draft, {"host": "different", "port": 3306, "database": "test"})

    def test_unavailable_record_has_normal_fallback_and_no_new_occupancy(self):
        service, compiler = self.service()
        result = service.begin_edit_session("ActionOne", handoff_ref={"handoff_id": "a" * 32, "sha256": "b" * 64})
        self.assertFalse(result["session_created"])
        self.assertEqual(result["handoff_status"], "unavailable")
        service.state.create_session.assert_not_called()
        compiler.describe_registry.assert_not_called()

    def test_source_draft_difference_is_not_silently_rebased(self):
        diagnosed = copy.deepcopy(self.data)
        diagnosed["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["value"] = "published"
        self.record["diagnosis_source"].update(version="published", design_id="published-id", data_sha256=sha256_text(json.dumps(diagnosed)))
        self.record["diagnosis_targets"] = snapshot_targets(diagnosed, self.targets)
        self.record["source_target_differences"] = compare_targets(self.record["diagnosis_targets"], self.record["targets"])
        service, compiler = self.service()
        result = service.begin_edit_session("ActionOne", handoff_ref=self.export())
        self.assertEqual(result["handoff_status"], "source_target_diverged")
        self.assertEqual(result["handoff"]["change_spec"], self.record["change_spec"])
        self.assertFalse(result["handoff"]["approval_inherited"])
        service.state.create_session.assert_not_called()
        compiler.compile_patch.assert_not_called()

    def test_tampering_is_detected_without_creating_session(self):
        reference = self.export()
        reference["sha256"] = "0" * 64
        service, compiler = self.service()
        result = service.begin_edit_session("ActionOne", handoff_ref=reference)
        self.assertEqual(result["error_code"], "HANDOFF_CHECKSUM")
        service.state.create_session.assert_not_called()

    def test_readiness_exception_does_not_leave_new_occupancy(self):
        service, compiler = self.service()
        compiler.describe_registry.return_value = {"elements": [{"key": "Noop"}], "bridge_sha256": "a" * 64}
        compiler.compiler_binding.return_value = {"compiler_binding_sha256": "b" * 64}
        with patch("gxp_editor.service.approval_secret", return_value="test-secret"), \
                patch("gxp_editor.service.BackupStore") as store, \
                patch.object(service, "editor_status", side_effect=RuntimeError("readiness unavailable")):
            store.return_value.create.return_value = {"backup_id": "test", "files": {}}
            with self.assertRaisesRegex(RuntimeError, "readiness unavailable"):
                service.begin_edit_session("ActionOne")
        service.state.create_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
