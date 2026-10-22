from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import baseline
from gxp_editor.approval import save_artifact
from gxp_editor.execution_lock import (
    ApprovalWindowBusyError, ExecutionBusyError, approval_window_lock, execution_lock,
)
from gxp_editor.recovery import archive_edit_session
from gxp_editor.service import EditorService, EditorServiceError
from gxp_editor.state import StateError, StateStore


class ChatLocalReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = StateStore(self.root / "state.sqlite3")
        self.owner = self.store.create_conversation("owner")["conversation_id"]
        self.other = self.store.create_conversation("other")["conversation_id"]
        self.session = self.store.create_session(baseline(), "immutable-backup", "compiler", conversation_id=self.owner)
        self.plan = self.store.create_plan(self.session["id"], str(self.root / "plan.json"), "old", "new", "compiler", conversation_id=self.owner)
        self.path = self.root / "approval.json"
        save_artifact(self.path, {"execution_started": True, "approvals": {"one": {"approved": True}}})
        self.transaction = self.store.create_transaction(self.session["id"], self.plan, "write", str(self.path), conversation_id=self.owner)
        self.store.update_transaction(self.transaction, "failed", "uncertain old result")
        self.service = object.__new__(EditorService)
        self.service.state = self.store
        self.service._conversation_id = self.owner

    def snapshot(self):
        return self.store.cancellation_snapshot(self.session["id"], conversation_id=self.owner)

    def release(self, snapshot=None, confirmation="同意保留当前数据并释放上述本地流程", **overrides):
        snapshot = self.snapshot() if snapshot is None else snapshot
        parameters = {"state": self.store, "conversation_id": self.owner, "owner_revision": 0,
                      "expected_fingerprint": snapshot["fingerprint"], "user_confirmation": confirmation}
        parameters.update(overrides)
        with patch("gxp_editor.process_check.assert_approval_process_stopped"):
            return archive_edit_session(self.session["id"], **parameters)

    def test_chat_release_handles_corrupt_artifact_without_business_access_or_success_claim(self):
        self.path.write_text("bad-json", encoding="utf-8")
        before = self.path.read_bytes()
        preview = self.snapshot()
        with patch("gxp_editor.recovery.EditorRepository", side_effect=AssertionError("No business access")), \
             patch("gxp_editor.recovery.load_config", side_effect=AssertionError("No business configuration")):
            result = self.release(preview)
        self.assertEqual(result["outcome"], "closed_unverified")
        self.assertTrue(result["session_released"])
        self.assertFalse(result["verified"])
        self.assertFalse(result["business_data_written"])
        self.assertIsNone(self.store.active_session(self.owner))
        self.assertEqual(self.store.get_session(self.session["id"])["backup_id"], "immutable-backup")
        self.assertEqual(self.store.get_transaction(self.transaction)["state"], "closed_unverified")
        self.assertEqual(self.store.get_transaction(self.transaction)["message"], "uncertain old result")
        self.assertEqual(self.store.get_plan(self.plan)["status"], "cancelled")
        self.assertEqual(self.path.read_bytes(), before)
        with self.store._connection() as db:
            audit = json.loads(db.execute("SELECT snapshot_json FROM session_closures").fetchone()[0])
        self.assertEqual(audit["event"], "user_authorized_local_release")
        self.assertEqual(audit["confirmation_channel"], "chat")
        self.assertEqual(audit["user_confirmation"], "同意保留当前数据并释放上述本地流程")
        self.assertEqual(audit["confirmation_snapshot"], preview)
        self.assertFalse(audit["verified"])
        self.assertFalse(audit["business_data_written"])
        with self.assertRaises(StateError):
            self.store.claim_transaction(self.transaction)

    def test_chat_release_handles_missing_artifact(self):
        self.path.unlink()
        self.assertTrue(self.release()["session_released"])

    def test_chat_release_handles_legacy_pending_transaction_with_execution_flag(self):
        self.store.update_transaction(self.transaction, "awaiting_approval", "")
        preview = self.snapshot()
        with self.assertRaisesRegex(StateError, "already started"):
            self.store.abort_session(self.session["id"], conversation_id=self.owner, owner_revision=0)
        self.assertTrue(self.release(preview)["session_released"])
        self.assertEqual(self.store.get_transaction(self.transaction)["state"], "closed_unverified")

    def test_explicit_confirmation_is_required_without_mutation(self):
        for confirmation in ("", "  ", False, "x" * 2001):
            with self.subTest(confirmation=repr(confirmation)):
                with self.assertRaisesRegex(StateError, "明确确认"):
                    self.release(confirmation=confirmation)
                self.assertEqual(self.store.get_session(self.session["id"])["state"], "awaiting_approval")
                self.assertEqual(self.store.get_plan(self.plan)["status"], "frozen")

    def test_changed_transaction_message_or_plan_invalidates_confirmation(self):
        for changed in ("message", "plan"):
            preview = self.snapshot()
            if changed == "message":
                self.store.update_transaction(self.transaction, "failed", "newly observed result")
            else:
                self.store.update_plan_status(self.plan, "cancelled")
            with self.subTest(changed=changed), self.assertRaisesRegex(StateError, "已变化"):
                self.release(preview)
            self.assertEqual(self.store.get_session(self.session["id"])["state"], "awaiting_approval")

    def test_owner_mismatch_and_stale_owner_revision_cannot_release(self):
        preview = self.snapshot()
        for overrides in ({"conversation_id": self.other}, {"owner_revision": 1}):
            with self.subTest(overrides=overrides), self.assertRaises(StateError):
                self.release(preview, **overrides)
        self.store.transfer_session(self.session["id"], self.other, expected_owner=self.owner,
                                    expected_revision=0, expected_transaction_id=self.transaction)
        self.store.transfer_session(self.session["id"], self.owner, expected_owner=self.other,
                                    expected_revision=1, expected_transaction_id=self.transaction)
        with self.assertRaises(StateError):
            self.release(preview)
        self.assertEqual(self.store.get_session(self.session["id"])["state"], "awaiting_approval")

    def test_real_execution_and_window_locks_block_local_release(self):
        preview = self.snapshot()
        for lock, error in ((execution_lock, ExecutionBusyError), (approval_window_lock, ApprovalWindowBusyError)):
            with self.subTest(lock=lock.__name__), lock(self.path), self.assertRaises(error):
                self.release(preview)
            self.assertEqual(self.store.get_transaction(self.transaction)["state"], "failed")
            self.assertEqual(self.store.get_session(self.session["id"])["state"], "awaiting_approval")

    def test_legacy_approval_process_blocks_release_even_without_new_locks(self):
        preview = self.snapshot()
        with patch("gxp_editor.process_check.assert_approval_process_stopped", side_effect=ExecutionBusyError("legacy process alive")):
            with self.assertRaisesRegex(ExecutionBusyError, "legacy process alive"):
                archive_edit_session(self.session["id"], state=self.store, conversation_id=self.owner,
                                     owner_revision=0, expected_fingerprint=preview["fingerprint"], user_confirmation="同意释放上述流程")
        self.assertEqual(self.store.get_transaction(self.transaction)["state"], "failed")

    def test_change_after_outer_checks_is_rejected_inside_state_transaction(self):
        preview = self.snapshot()
        archive = self.store.archive_session
        def changed_then_archive(*args, **kwargs):
            self.store.update_transaction(self.transaction, "failed", "changed during lock checks")
            return archive(*args, **kwargs)
        with patch.object(self.store, "archive_session", side_effect=changed_then_archive):
            with self.assertRaisesRegex(StateError, "已变化"):
                self.release(preview)
        self.assertEqual(self.store.get_session(self.session["id"])["state"], "awaiting_approval")
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM session_closures").fetchone()[0], 0)

    def test_audit_and_release_roll_back_together_on_local_failure(self):
        with self.store._connection() as db:
            db.execute("CREATE TRIGGER reject_release BEFORE UPDATE ON sessions WHEN NEW.state='closed_unverified' "
                       "BEGIN SELECT RAISE(ABORT, 'test release rejection'); END")
        with self.assertRaisesRegex(Exception, "test release rejection"):
            self.release()
        self.assertEqual(self.store.get_transaction(self.transaction)["state"], "failed")
        self.assertEqual(self.store.get_plan(self.plan)["status"], "frozen")
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM session_closures").fetchone()[0], 0)

    def test_service_inspection_to_release_is_available_for_uncertain_corrupt_results(self):
        self.path.write_text("bad-json", encoding="utf-8")
        with patch.object(self.service, "_components", side_effect=AssertionError("No business connection")), \
             patch("gxp_editor.process_check.assert_approval_process_stopped"), \
             patch("gxp_editor.recovery.load_config", side_effect=AssertionError("No business configuration")):
            preview = self.service.inspect_session_cancellation(self.session["id"])
            self.assertFalse(preview["cancellable"])
            self.assertIsNone(preview["cancellation_fingerprint"])
            release = preview["local_release"]
            self.assertTrue(release["available"])
            self.assertTrue(release["requires_user_confirmation"])
            self.assertTrue(release["requires_idle_executor"])
            self.assertEqual(release["backup_id"], "immutable-backup")
            self.assertEqual(release["transactions"], [{"id": self.transaction, "kind": "write", "state": "failed"}])
            result = self.service.release_local_session(self.session["id"], release["fingerprint"], "同意保留数据并释放该流程")
        self.assertTrue(result["session_released"])
        self.assertTrue(result["backups_retained"])
        self.assertTrue(result["audit_retained"])
        self.assertTrue(result["retry_requires_new_plan"])
        self.assertFalse(result["verified"])
        self.assertFalse(result["write_executed"])
        self.assertEqual(result["outcome"], "closed_unverified")
        self.assertIsNone(self.store.active_session(self.owner))
        after = self.service.inspect_session_cancellation(self.session["id"])
        self.assertFalse(after["local_release"]["available"])
        self.assertIsNone(after["local_release"]["fingerprint"])

    def test_service_rejects_missing_confirmation_and_invalid_token_types(self):
        token = self.service.inspect_session_cancellation(self.session["id"])["local_release"]["fingerprint"]
        for confirmation in ("", "  ", False, None, "x" * 2001):
            with self.subTest(confirmation=repr(confirmation)), self.assertRaises(EditorServiceError):
                self.service.release_local_session(self.session["id"], token, confirmation)
        for invalid in (None, False, 1, {}, [], "", "x" * 63, "x" * 64, "x" * 65):
            with self.subTest(token=invalid), self.assertRaises(EditorServiceError):
                self.service.release_local_session(self.session["id"], invalid, "同意释放上述流程")
        self.assertEqual(self.store.get_transaction(self.transaction)["state"], "failed")

    def test_cancel_and_release_tokens_are_operation_specific(self):
        self.store.update_transaction(self.transaction, "awaiting_approval", "")
        save_artifact(self.path, {})
        preview = self.service.inspect_session_cancellation(self.session["id"])
        self.assertTrue(preview["cancellable"])
        self.assertTrue(preview["local_release"]["available"])
        self.assertNotEqual(preview["cancellation_fingerprint"], preview["local_release"]["fingerprint"])
        with self.assertRaises(EditorServiceError):
            self.service.release_local_session(self.session["id"], preview["cancellation_fingerprint"], "同意取消")
        with self.assertRaises(EditorServiceError):
            self.service.cancel_unexecuted_session(self.session["id"], preview["local_release"]["fingerprint"], "同意释放")
        self.assertEqual(self.store.get_session(self.session["id"])["state"], "awaiting_approval")

    def test_service_changed_fingerprint_or_session_owner_blocks_release(self):
        token = self.service.inspect_session_cancellation(self.session["id"])["local_release"]["fingerprint"]
        self.store.update_transaction(self.transaction, "failed", "new result observed")
        with self.assertRaisesRegex(EditorServiceError, "已变化"):
            self.service.release_local_session(self.session["id"], token, "同意释放")
        token = self.service.inspect_session_cancellation(self.session["id"])["local_release"]["fingerprint"]
        self.service._conversation_id = self.other
        with self.assertRaises(StateError):
            self.service.release_local_session(self.session["id"], token, "同意释放")
        self.service._conversation_id = self.owner
        self.store.transfer_session(self.session["id"], self.other, expected_owner=self.owner,
                                    expected_revision=0, expected_transaction_id=self.transaction)
        self.store.transfer_session(self.session["id"], self.owner, expected_owner=self.other,
                                    expected_revision=1, expected_transaction_id=self.transaction)
        with self.assertRaisesRegex(EditorServiceError, "已变化"):
            self.service.release_local_session(self.session["id"], token, "同意释放")
        self.assertEqual(self.store.get_transaction(self.transaction)["state"], "failed")


if __name__ == "__main__":
    unittest.main()
