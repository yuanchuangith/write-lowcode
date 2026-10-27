from __future__ import annotations

import base64
import copy
import json
import unittest
from unittest.mock import patch

from tests.payload_fixtures import PayloadHarness
from gxp_editor.approval import load_artifact
from gxp_editor.compiler import CompilerBridge, CompilerError
from gxp_editor.edit_context import EditContextError, build_edit_context
from gxp_editor.edit_guards import EditGuardError, create_guard_ref, expand_guard_operations, parameter_value
from gxp_editor.hashing import canonical_json, sha256_json
from gxp_editor.logic_review import plan_binding_hash
from gxp_editor.review_response import review_page
from gxp_editor.service import EditorServiceError
from gxp_editor.value_transport import value_page


class PayloadReductionTests(unittest.TestCase):
    def setUp(self):
        self.h = self.enterContext(PayloadHarness())
        self.service = self.h.service
        self.session_id = self.service.begin_edit_session("ActionOne")["session_id"]
        self.targets = [{"group_key": "main", "node_key": "target"}]
        self.path = "/inputParams/settings/flag"

    def context(self, **kwargs):
        return self.service.inspect_edit_context(self.session_id, self.targets, response_detail="summary", **kwargs)

    def operation(self, value=True):
        context = self.context(parameter_paths=[self.path])
        return {**context["nodes"][0]["edit_options"]["parameter_operations"][0]["template"], "value": value}

    def test_summary_and_legacy_full_projections_keep_exact_identifiers(self):
        result = self.context(parameter_paths=[self.path])
        node = result["nodes"][0]
        self.assertEqual(result["context_detail"], "summary")
        self.assertNotIn("params_value", node)
        self.assertNotIn("visible_variables", node)
        self.assertEqual(node["parameters"][0]["value"], False)
        operation = node["edit_options"]["parameter_operations"][0]
        self.assertEqual(operation["required_inputs"], ["value"])
        self.assertIn("guard_ref", operation["template"])
        self.assertEqual(self.service.inspect_edit_context(self.session_id, self.targets)["context_detail"], "full_context")
        self.assertEqual(self.context(include_context=True)["context_detail"], "full_context")
        self.assertEqual(self.context(include_context=False)["context_detail"], "compact_guards")

    def test_guard_expands_before_strict_preflight_and_native_compilation(self):
        operation = self.operation()
        seen = []
        original = CompilerBridge.preflight_operations
        def preflight(design, operations):
            seen.extend(copy.deepcopy(operations))
            return original(design, operations)
        with patch.object(CompilerBridge, "preflight_operations", side_effect=preflight):
            planned = self.service.plan_canvas_patch(self.session_id, [operation], self.h.spec(), response_detail="summary")
        self.assertTrue(planned["ready_for_approval"])
        self.assertFalse(seen[0]["expected_old_value"])
        self.assertNotIn("guard_ref", seen[0])
        self.assertEqual(self.h.compiler.seen_operations[0], seen)
        artifact = load_artifact(self.service.state.get_plan(planned["plan_id"])["artifact_path"])
        self.assertNotIn("guard_ref", json.dumps(artifact["operations"]))
        self.assertEqual(artifact["operations"], seen)

    def test_legacy_and_reference_operations_produce_identical_bound_plan_evidence(self):
        guarded = self.operation()
        plain = {key: value for key, value in guarded.items() if key != "guard_ref"}
        plain["expected_old_value"] = False
        first = self.service.plan_canvas_patch(self.session_id, [plain], self.h.spec())
        a = load_artifact(self.service.state.get_plan(first["plan_id"])["artifact_path"])
        second = self.service.plan_canvas_patch(self.session_id, [guarded])
        b = load_artifact(self.service.state.get_plan(second["plan_id"])["artifact_path"])
        self.assertEqual(a["desired"], b["desired"])
        self.assertEqual(a["business_review"], b["business_review"])
        self.assertEqual(plan_binding_hash(a), plan_binding_hash(b))
        pending = self.service.prepare_write(second["plan_id"], response_detail="summary")
        tx = self.service.state.get_transaction(pending["transaction_id"])
        approval = load_artifact(tx["artifact_path"])
        self.assertEqual(approval["business_review"], b["business_review"])
        self.assertEqual(approval["desired_design"], {key: b["desired"][key] for key in ("data", "js_code", "csharp_code")})
        self.assertEqual(approval["approvals"], {})

    def test_tampering_key_rotation_wrong_path_and_double_guard_reject(self):
        operation = self.operation()
        mutations = [lambda op: op.update(guard_ref=op["guard_ref"][:-1] + ("0" if op["guard_ref"][-1] != "0" else "1")),
                     lambda op: op.update(path="/inputParams/settings/notes"),
                     lambda op: op.update(target_node_key="missing"),
                     lambda op: op.update(expected_old_value=False),
                     lambda op: op.update(operation="replace_params")]
        session = self.service.state.get_session(self.session_id)
        for mutate in mutations:
            changed = copy.deepcopy(operation)
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(EditGuardError):
                expand_guard_operations(session, self.h.owner, self.h.canvas, [changed], "synthetic-guard-secret")
        with self.assertRaises(EditGuardError):
            expand_guard_operations(session, self.h.owner, self.h.canvas, [operation], "rotated")
        # A correctly re-encoded but edited payload still needs the HMAC.
        encoded, signature = operation["guard_ref"].split(".")
        raw = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        raw["purpose"] = "approval"
        forged = base64.urlsafe_b64encode(canonical_json(raw).encode()).decode().rstrip("=") + "." + signature
        with self.assertRaises(EditGuardError):
            expand_guard_operations(session, self.h.owner, self.h.canvas, [{**operation, "guard_ref": forged}], "synthetic-guard-secret")

    def test_session_binding_changes_invalidate_reference_and_no_changes_checks_compiler(self):
        operation = self.operation(False)
        session = self.service.state.get_session(self.session_id)
        for field, value in (("id", "other"), ("owner_revision", 2), ("backup_id", "other"),
                             ("compiler_hash", "other"), ("design_id", "other")):
            changed = {**session, field: value}
            with self.subTest(field=field), self.assertRaises(EditGuardError):
                expand_guard_operations(changed, self.h.owner, self.h.canvas, [operation], "synthetic-guard-secret")
        for field, value in (("content_hash", "changed"), ("relations", [{"id": "changed"}])):
            changed = copy.deepcopy(session)
            changed["baseline"][field] = value
            with self.subTest(baseline_field=field), self.assertRaises(EditGuardError):
                expand_guard_operations(changed, self.h.owner, self.h.canvas, [operation], "synthetic-guard-secret")
        with self.assertRaises(EditGuardError):
            expand_guard_operations(session, "another-dialogue", self.h.canvas, [operation], "synthetic-guard-secret")
        with patch.object(self.h.compiler, "registry_and_binding", return_value=({}, {"compiler_binding_sha256": "changed"})):
            with self.assertRaisesRegex(EditorServiceError, "compiler source"):
                self.service.plan_canvas_patch(self.session_id, [operation], self.h.spec())
        result = self.service.plan_canvas_patch(self.session_id, [operation], self.h.spec())
        self.assertTrue(result["no_changes"])

    def test_array_selector_reference_is_exact_and_preserves_sequential_guards(self):
        session = self.service.state.get_session(self.session_id)
        design = copy.deepcopy(self.h.canvas)
        design["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["rows"] = [{"id": "a", "value": 1}, {"id": "b", "value": 2}]
        operation = {"operation": "set_array_item", "action_group_key": "main", "target_node_key": "target",
            "expected_element_key": "Noop", "path": "/inputParams/rows", "item_key": {"field": "id", "value": "a"},
            "value": {"id": "a", "value": 3}}
        operation["guard_ref"] = create_guard_ref(session, self.h.owner, operation, {"id": "a", "value": 1}, "synthetic-guard-secret")
        expanded = expand_guard_operations(session, self.h.owner, design, [operation], "synthetic-guard-secret")
        self.assertEqual(expanded[0]["expected_old_value"], {"id": "a", "value": 1})
        with self.assertRaises(EditGuardError):
            expand_guard_operations(session, self.h.owner, design, [{**operation, "item_key": {"field": "id", "value": "b"}}], "synthetic-guard-secret")
        with self.assertRaises(CompilerError):
            CompilerBridge.preflight_operations(design, expanded * 2)

    def test_value_pages_recheck_live_draft_relations_and_owner(self):
        result = self.service.read_edit_value(self.session_id, "main", "target", self.path)
        self.assertIs(result["value"], False)
        self.assertTrue(result["complete"])
        self.assertIn("guard_ref", result)
        self.h.draft["content_hash"] = "changed"
        with self.assertRaisesRegex(EditorServiceError, "stale"):
            self.service.read_edit_value(self.session_id, "main", "target", self.path)

    def test_value_pages_reject_relation_drift_and_transfer_before_return(self):
        with patch.object(self.h.repository, "load_relations", return_value=[{"id": "changed"}]):
            with self.assertRaisesRegex(EditorServiceError, "relations changed"):
                self.service.read_edit_value(self.session_id, "main", "target", self.path)
        other = self.service.state.create_conversation("other")['conversation_id']
        original = value_page
        def transfer_during_paging(*args, **kwargs):
            result = original(*args, **kwargs)
            self.service.state.transfer_session(self.session_id, other, expected_owner=self.h.owner, expected_revision=0)
            self.service.state.transfer_session(self.session_id, self.h.owner, expected_owner=other, expected_revision=1)
            return result
        with patch("gxp_editor.value_transport.value_page", side_effect=transfer_during_paging):
            with self.assertRaisesRegex(EditorServiceError, "ownership or active state changed"):
                self.service.read_edit_value(self.session_id, "main", "target", self.path)

    def test_value_pages_reject_session_finishing_during_paging(self):
        original = value_page
        def finish_during_paging(*args, **kwargs):
            result = original(*args, **kwargs)
            self.service.state.update_session(self.session_id, "finished", conversation_id=self.h.owner, owner_revision=0)
            return result
        with patch("gxp_editor.value_transport.value_page", side_effect=finish_during_paging):
            with self.assertRaisesRegex(EditorServiceError, "active state changed"):
                self.service.read_edit_value(self.session_id, "main", "target", self.path)

    def test_unknown_parameter_paths_and_array_indexes_do_not_invent_guards(self):
        for paths in (["/inputParams/missing"], ["/inputParams/settings/0"], ["/inputParams/settings/flag"] * 2, []):
            with self.subTest(paths=paths), self.assertRaises(EditContextError):
                self.context(parameter_paths=paths)


class ValuePagingTests(unittest.TestCase):
    def test_offset_counts_characters_inside_chinese_and_emoji(self):
        value = "中🙂文" * 1000
        page = value_page(value, offset=2, max_output_bytes=1024)
        self.assertTrue(page["text"].startswith("🙂文中"))
        self.assertEqual(page["next_offset"], 2 + len(page["text"]))
        self.assertEqual(page["total_chars"], len(canonical_json(value)))

    def test_large_unicode_json_round_trips_exactly_with_bounded_fragments(self):
        value = {"text": "中文\n\"\\🙂" * 2000, "enabled": False}
        offset, pieces = 0, []
        while True:
            page = value_page(value, offset=offset, max_output_bytes=4096)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 4096)
            self.assertFalse(page["complete"])
            self.assertTrue(page["truncated"])
            self.assertEqual(page["value_sha256"], sha256_json(value))
            self.assertEqual(page["offset_unit"], "unicode_code_points")
            pieces.append(page["text"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            self.assertEqual(page["next_offset"], offset + len(page["text"]))
            offset = page["next_offset"]
        self.assertEqual(json.loads("".join(pieces)), value)

    def test_invalid_budget_offset_and_noninteger_offsets_reject(self):
        for kwargs in ({"offset": True}, {"offset": -1}, {"offset": 999}, {"offset": 1.5},
                       {"max_output_bytes": 100}, {"max_output_bytes": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                value_page("中文", **kwargs)

    def test_review_summary_and_field_continuation_preserve_complete_local_evidence(self):
        row = {"id": "big", "status": "unverified", "before": {"text": "中文" * 20000}, "after": {"text": "new"}}
        report = {"changes": [row], "requirements": [], "scenarios": {"cases": []}, "spec": {}}
        snapshot = copy.deepcopy(report)
        summary = review_page(report, "changes", detail="summary")
        self.assertNotIn("before", summary["items"][0])
        self.assertEqual(summary["items"][0]["fields"][0]["field_path"], "/before")
        first = review_page(report, "changes", item_id="big", detail="summary", field_path="/before/text", max_output_bytes=4096)
        self.assertFalse(first["complete"])
        self.assertLessEqual(len(json.dumps(first, ensure_ascii=False).encode()), 4096)
        self.assertEqual(first["next_offset"], first["evidence_gaps"][0]["next_read"]["arguments"]["value_offset"])
        last = review_page(report, "changes", item_id="big", field_path="/before/text",
                           value_offset=first["total_chars"] - 4, max_output_bytes=4096)
        self.assertFalse(last["complete"])
        self.assertIsNone(last["evidence_gaps"][0]["next_read"])
        self.assertEqual("verify_fragment_assembly", last["fallback"]["stop_reason"])
        self.assertEqual(review_page(report, "changes", detail="full")["items"], [row])
        self.assertEqual(report, snapshot)

    def test_generation_summary_replaces_unbounded_key_lists(self):
        from gxp_editor.service import EditorService
        original = {"scope": {"affected_group_keys": [str(i) for i in range(1000)]}, "selected_mode": "incremental"}
        result = EditorService._generation_summary(original)
        self.assertEqual(result["scope"]["affected_group_keys"]["count"], 1000)
        self.assertEqual(len(result["scope"]["affected_group_keys"]["preview"]), 5)
        self.assertEqual(len(original["scope"]["affected_group_keys"]), 1000)


if __name__ == "__main__":
    unittest.main()
