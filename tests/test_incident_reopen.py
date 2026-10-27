"""Applicability failures never reopen a window or rewrite an approval."""
import unittest
from unittest.mock import patch

from tests import test_recovery_service as recovery_fixture
from tests.helpers import baseline, relation
from gxp_editor.approval import save_artifact, transaction_binding_hash


class IncidentReopenTests(unittest.TestCase):
    setUp = recovery_fixture.RecoveryServiceTests.setUp
    def test_stale_payload_revision_relations_and_target_are_specific(self):
        cases = [(baseline(js_code="changed"), [], "draft_baseline_changed", "js_code_changed"),
                 (baseline(last_modification_time="2026-07-26 09:00:00.000"), [], "draft_baseline_changed", "revision_only_changed"),
                 (baseline(), [relation()], "draft_relations_changed", "relations_changed"),
                 (baseline(is_publish=1), [], "draft_baseline_changed", "target_publish_state_changed")]
        for current, rows, code, category in cases:
            with self.subTest(category=category):
                self.repository.load_draft_by_id.return_value = current
                self.repository._relations.return_value = rows
                result = self.service.reopen_approval(self.tx)
                self.assertEqual(result["reason_code"], code)
                self.assertFalse(result["execution_enabled"])
                self.assertEqual(self.store.get_transaction(self.tx)["state"], "awaiting_approval")
                self.assertFalse(result["approval_window_opened"])
        self.launch.assert_not_called()

    def test_snapshot_read_failure_is_unknown_and_no_old_evidence_is_used(self):
        self.repository._relations.side_effect = TimeoutError("synthetic timeout")
        original = self.path.read_bytes()
        result = self.service.reopen_approval(self.tx)
        self.assertEqual(result["reason_code"], "baseline_evidence_unavailable")
        self.assertEqual(result["preflight_status"], "unknown")
        self.assertEqual(self.path.read_bytes(), original)
        self.launch.assert_not_called()

    def test_one_read_transaction_supplies_current_draft_and_relations(self):
        result = self.service.reopen_approval(self.tx)
        self.assertTrue(result["approval_window_opened"])
        self.repository.load_draft_by_id.assert_called_once_with("design-1", session=self.db_session)
        self.repository._relations.assert_called_once_with("design-1", self.db_session)
        self.assertEqual(result["preflight"]["principal_verification"], "legacy_grants_only")

    def test_bound_principal_and_compiler_drift_block_but_legacy_does_not_get_resigned(self):
        legacy = self.path.read_bytes()
        self.assertTrue(self.service.reopen_approval(self.tx)["approval_window_opened"])
        self.assertEqual(self.path.read_bytes(), legacy)
        self.artifact["database_safety"]["authenticated_principal"] = "actor@host"
        self.artifact["transaction_hash"] = transaction_binding_hash(self.artifact)
        save_artifact(self.path, self.artifact)
        self.launch.reset_mock()
        self.repository.grant_status.return_value.update(authenticated_principal="other@host")
        result = self.service.reopen_approval(self.tx)
        self.assertFalse(result["approval_window_opened"])
        self.launch.assert_not_called()
        self.repository.grant_status.return_value["authenticated_principal"] = "actor@host"
        self.compiler.compiler_binding.return_value = {"compiler_binding_sha256": "0" * 64}
        result = self.service.reopen_approval(self.tx)
        self.assertEqual(result["reason_code"], "compiler_binding_changed")

    def test_grants_drift_blocks_and_does_not_consume_approvals(self):
        self.repository.grant_status.return_value["grants_sha256"] = "d" * 64
        before = self.path.read_bytes()
        result = self.service.reopen_approval(self.tx)
        self.assertEqual(result["reason_code"], "database_safety_changed")
        self.assertEqual(before, self.path.read_bytes())
        self.launch.assert_not_called()

    def test_attach_reports_unknown_after_successful_transfer(self):
        owner = self.store.create_conversation("new owner")["conversation_id"]
        self.service._conversation_id = owner
        self.repository._relations.side_effect = TimeoutError()
        with patch("gxp_editor.process_check.assert_approval_process_stopped"):
            result = self.service.attach_edit_session(self.session["id"])
        self.assertTrue(result["attached"])
        self.assertEqual(result["baseline_status"], "unknown")
        self.assertEqual(self.store.get_session(self.session["id"])["conversation_id"], owner)

    def test_dead_queue_reserves_generation_and_reuses_pending_transaction(self):
        owner = self.store.create_conversation("queue owner")["conversation_id"]
        queue = self.store.create_edit_queue(owner)
        # Synthetic fixture ownership only; this does not touch a real session.
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET conversation_id=?,queue_id=? WHERE id=?", (owner, queue, self.session["id"]))
            db.execute("INSERT INTO edit_queue_items VALUES (?,?,?,?,?)", (queue, self.session["id"], 0, 0, "{}"))
        self.service._conversation_id = owner
        self.startup.return_value = {"startup_status": "failed", "process_state": "exited"}
        from gxp_editor.service import EditorService
        from gxp_editor.queue_protocol import queue_snapshot, TOKEN_ENV, GENERATION_ENV
        with patch.object(self.service, "_launch_approval", wraps=EditorService._launch_approval.__get__(self.service)), patch(
                "gxp_editor.ui_launch.launch_window", return_value={"window_opened": True, "startup_status": "ready"}) as launch:
            result = self.service.reopen_approval(self.tx)
        self.assertTrue(result["approval_window_opened"])
        self.assertEqual(self.store.get_session(self.session["id"])["transaction_id"], self.tx)
        view = queue_snapshot(self.store, queue, owner)
        self.assertEqual(view["generation"], 1)
        self.assertEqual(launch.call_args.kwargs["extra_env"][TOKEN_ENV], view["launch_token"])
        self.assertEqual(launch.call_args.kwargs["extra_env"][GENERATION_ENV], "1")

    def test_mcp_transport_preserves_comparison_code_and_no_payload(self):
        import asyncio
        from tests.test_conversation_tools import SERVER
        from gxp_editor.service import EditorService
        current = baseline(js_code="sensitive synthetic code")
        self.repository.load_draft_by_id.return_value = current
        # Exercise a real service guard via the actual SDK output converter.
        with patch.object(SERVER, "service", return_value=self.service):
            _, result = asyncio.run(SERVER.mcp.call_tool("inspect_edit_context",
                {"session_id": self.session["id"], "targets": []}))
        self.assertEqual(result["error_code"], "draft_baseline_changed")
        self.assertEqual(result["error_details"]["status"], "stale")
        self.assertNotIn("sensitive synthetic code", str(result))

    def test_restore_reopen_keeps_original_checkpoint_without_new_write_review(self):
        from tests.helpers import transaction
        from gxp_editor.service import EditorService
        restored = transaction(kind="restore", desired_relations=[])
        restored.update(transaction_id=self.tx, session_id=self.session["id"], plan_id=None)
        restored["transaction_hash"] = transaction_binding_hash(restored)
        save_artifact(self.path, restored)
        with self.store._connection() as db:
            db.execute("UPDATE transactions SET kind='restore',plan_id=NULL WHERE id=?", (self.tx,))
            db.execute("UPDATE sessions SET state='restore_pending' WHERE id=?", (self.session["id"],))
        with patch.object(self.store, "review_spec_checkpoint", return_value={"spec": {"old_write_scope": True}}):
            result = self.service.reopen_approval(self.tx)
        self.assertTrue(result["approval_window_opened"])
        self.compiler.compiler_binding.assert_not_called()

    def test_artifact_changed_after_preflight_is_not_launched(self):
        from gxp_editor.service import EditorService
        from gxp_editor.hashing import sha256_json
        fingerprint = sha256_json(self.artifact)
        self.artifact["created_at"] = "changed after preflight"
        save_artifact(self.path, self.artifact)
        with patch("gxp_editor.ui_launch.launch_window") as launch:
            with self.assertRaisesRegex(Exception, "artifact changed"):
                EditorService._launch_approval(self.service, self.tx, approval_fingerprint=fingerprint)
        launch.assert_not_called()
