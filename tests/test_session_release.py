from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gxp_editor.approval import save_artifact
from gxp_editor.execution_lock import ApprovalWindowBusyError, ExecutionBusyError, approval_window_lock, execution_lock
from gxp_editor.recovery import archive_edit_session
from gxp_editor.process_check import approval_process_ids, assert_approval_process_stopped
from gxp_editor.service import EditorService
from gxp_editor.state import StateError, StateStore
from tests.helpers import PLUGIN_ROOT, baseline


class SessionReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = StateStore(self.root / "state.sqlite3")
        self.owner = self.store.create_conversation()["conversation_id"]
        self.session = self.store.create_session(baseline(), "backup", "compiler", conversation_id=self.owner)
        self.plan = self.store.create_plan(self.session["id"], "plan.json", "old", "new", "compiler", conversation_id=self.owner)
        self.path = self.root / "transactions" / "approval.json"
        self.path.parent.mkdir()
        save_artifact(self.path, {})
        self.tx = self.store.create_transaction(self.session["id"], self.plan, "write", str(self.path), conversation_id=self.owner)
        patcher = patch("gxp_editor.process_check.assert_approval_process_stopped")
        self.process_check = patcher.start()
        self.addCleanup(patcher.stop)
        self.service = object.__new__(EditorService)
        self.service.state = self.store
        self.service._conversation_id = self.owner

    def archive(self, **kwargs):
        return archive_edit_session(self.session["id"], state=self.store, conversation_id=self.owner, owner_revision=0, **kwargs)

    def test_missing_corrupt_or_legacy_artifact_does_not_block_manual_release(self):
        for content in (None, "broken-json", '{"execution_started":true}', "[]"):
            with self.subTest(content=content):
                if content is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.write_text(content, encoding="utf-8")
                self.store.update_transaction(self.tx, "executing")
                self.store.update_session(self.session["id"], "awaiting_approval", conversation_id=self.owner)
                self.assertTrue(self.archive()["session_released"])
                self.assertIsNone(self.store.active_session(self.owner))
                self.assertEqual(self.store.get_transaction(self.tx)["state"], "closed_unverified")
                with self.assertRaises(StateError):
                    self.store.claim_transaction(self.tx)
        with self.store._connection() as db:
            events = list(db.execute("SELECT snapshot_json FROM session_closures"))
        self.assertEqual(len(events), 4)
        for event in events:
            self.assertFalse(json.loads(event[0])["verified"])

    def test_release_does_not_connect_to_database_or_read_signed_artifact(self):
        with patch("gxp_editor.recovery.load_config", side_effect=RuntimeError("offline")) as config, \
             patch("gxp_editor.recovery.load_artifact", side_effect=ValueError("corrupt")) as artifact, \
             patch("gxp_editor.recovery.EditorRepository") as repository:
            self.assertTrue(self.archive()["session_released"])
            config.assert_not_called()
            artifact.assert_not_called()
            repository.assert_not_called()
        self.assertEqual(self.store.get_session(self.session["id"])["backup_id"], "backup")

    def test_saved_history_is_preserved_without_asserting_verified_success(self):
        self.store.update_transaction(self.tx, "saved", "original result")
        self.store.update_session(self.session["id"], "saved", conversation_id=self.owner)
        result = self.archive()
        self.assertFalse(result["verified"])
        self.assertEqual(self.store.get_transaction(self.tx)["state"], "saved")
        self.assertEqual(self.store.get_transaction(self.tx)["message"], "original result")
        self.assertEqual(self.store.get_session(self.session["id"])["state"], "closed_unverified")

    def test_window_executor_and_legacy_process_each_block_release(self):
        with approval_window_lock(self.path), self.assertRaises(ApprovalWindowBusyError):
            self.archive()
        with execution_lock(self.path), self.assertRaises(ExecutionBusyError):
            self.archive()
        self.process_check.side_effect = ExecutionBusyError("legacy process alive")
        with self.assertRaises(ExecutionBusyError):
            self.archive()
        self.assertIsNotNone(self.store.active_session(self.owner))

    def test_process_check_failure_does_not_release(self):
        self.process_check.side_effect = ExecutionBusyError("process state unavailable")
        with self.assertRaises(ExecutionBusyError):
            self.archive()
        self.assertEqual(self.store.get_transaction(self.tx)["state"], "awaiting_approval")

    def test_deleted_artifact_directory_does_not_block_locking_and_release(self):
        self.path.unlink()
        self.path.parent.rmdir()
        self.assertTrue(self.archive()["session_released"])

    def test_corrupt_baseline_does_not_block_handling_window_or_release(self):
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET baseline_json='bad-json' WHERE id=?", (self.session["id"],))
        with patch("gxp_editor.ui_launch.launch_window", return_value={"window_opened": True, "window_starting": False}) as launch:
            result = self.service.abort_edit_session(self.session["id"])
            self.assertTrue(result["confirmation_window_opened"])
            self.assertFalse(result["confirmation_window_starting"])
            launch.assert_called_once()
            self.assertEqual(launch.call_args.kwargs["arguments"], [self.session["id"], self.owner, "0"])
        self.assertTrue(self.archive()["session_released"])

    def test_missing_transaction_record_can_be_archived(self):
        with self.store._connection() as db:
            db.execute("DELETE FROM transactions WHERE id=?", (self.tx,))
        with patch("gxp_editor.recovery.runtime_root", return_value=self.root):
            self.assertTrue(self.archive()["session_released"])
        self.process_check.assert_called_once_with(self.tx)

    def test_status_is_available_when_artifact_is_missing_or_not_an_object(self):
        for content in (None, "[]", "not-json"):
            with self.subTest(content=content):
                if content is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.write_text(content, encoding="utf-8")
                status = self.service.get_write_status(self.tx)
                self.assertFalse(status["artifact_available"])
                self.assertTrue(status["handling_available"])
                self.assertEqual(status["state"], "awaiting_approval")

    def test_completed_save_queries_reuse_receipt_after_platform_changes(self):
        self.store.update_transaction(self.tx, "saved")
        self.store.update_session(self.session["id"], "saved", conversation_id=self.owner)
        receipt = self.store.finish_verified_save(
            self.tx, content_hash="approved-content", relation_hash="approved-relations",
            conversation_id=self.owner, owner_revision=0,
        )
        # No current database or compiler access is needed for this historical
        # result, even if the platform has since regenerated/published its code.
        with patch.object(self.service, "_components", side_effect=AssertionError("must not recheck current draft")):
            status = self.service.get_write_status(self.tx)
            self.assertTrue(status["verified"])
            self.assertTrue(status["session_released"])
            review = status["look_graph_review"]
            self.assertEqual(review["transaction_id"], self.tx)
            self.assertTrue(review["compare_before_asking"])
            self.assertTrue(review["ask_only_if_changes"])
            self.assertEqual(review["supported_changes"], ["add", "update"])
            self.assertTrue(review["requires_user_confirmation"])
            self.assertFalse(review["graph_write_executed"])
            self.assertEqual(status["publication_targets"][0]["action_name"], "Action one")
            self.assertEqual(status["publication_targets"][0]["action_code"], "ActionOne")
            verified = self.service.verify_saved_draft(self.session["id"])
            self.assertEqual(verified["saved_verification"], receipt)
            self.assertFalse(verified["current_draft_rechecked"])
            self.assertEqual(verified["look_graph_review"], review)
            self.assertEqual(verified["publication_targets"][0]["action_code"], "ActionOne")
            finished = self.service.finish_edit_session(self.session["id"])
            self.assertTrue(finished["finished"])
            self.assertEqual(finished["look_graph_review"], review)
            self.assertEqual(finished["publication_targets"][0]["ref_id"], "ref-1")
        self.path.unlink()
        status = self.service.get_write_status(self.tx)
        self.assertFalse(status["artifact_available"])
        self.assertEqual(status["publication_targets"][0]["action_code"], "ActionOne")
        self.assertIsNone(self.store.active_session(self.owner))

    def test_graph_review_waits_for_verified_completion_and_excludes_unsuccessful_closures(self):
        for transaction_state, session_state in (
            ("awaiting_approval", "awaiting_approval"),
            ("executing", "awaiting_approval"),
            ("saved", "saved"),
            ("saved", "verified"),
            ("saved_state_pending", "saved_state_pending"),
            ("rolled_back", "rolled_back"),
            ("failed_no_write", "failed_no_write"),
            ("cancelled", "aborted"),
            ("saved", "closed_unverified"),
        ):
            with self.subTest(transaction=transaction_state, session=session_state):
                self.store.update_transaction(self.tx, transaction_state)
                self.store.update_session(self.session["id"], session_state, conversation_id=self.owner)
                self.assertIsNone(self.service.get_write_status(self.tx)["look_graph_review"])

    def test_manual_finish_adds_graph_review_without_reexecuting_saved_transaction(self):
        self.store.update_transaction(self.tx, "saved")
        self.store.update_session(self.session["id"], "verified", conversation_id=self.owner)
        with patch.object(self.service, "_components", side_effect=AssertionError("must not execute")):
            result = self.service.finish_edit_session(self.session["id"])
            self.assertEqual(result["look_graph_review"]["transaction_id"], self.tx)
            self.assertEqual(result["look_graph_review"], self.service.get_write_status(self.tx)["look_graph_review"])
        self.assertEqual(self.store.get_transaction(self.tx)["state"], "saved")

    def test_previous_save_does_not_offer_graph_review_during_a_new_restore(self):
        self.store.update_transaction(self.tx, "saved")
        self.store.update_session(self.session["id"], "verified", conversation_id=self.owner)
        restore = self.store.create_transaction(self.session["id"], None, "restore", "restore.json", conversation_id=self.owner)
        self.assertIsNone(self.service.get_write_status(self.tx)["look_graph_review"])
        self.store.update_transaction(restore, "saved")
        self.store.update_session(self.session["id"], "verified", conversation_id=self.owner)
        result = self.service.finish_edit_session(self.session["id"])
        self.assertEqual(result["look_graph_review"]["transaction_id"], restore)
        self.assertIsNone(self.service.get_write_status(self.tx)["look_graph_review"])

    def test_unverified_closure_is_never_reported_as_verified(self):
        self.store.update_transaction(self.tx, "saved")
        self.store.update_session(self.session["id"], "saved", conversation_id=self.owner)
        self.archive()
        status = self.service.get_write_status(self.tx)
        self.assertFalse(status["verified"])
        self.assertIsNone(status["saved_verification"])
        self.assertIsNone(status["look_graph_review"])
        self.assertEqual(status["publication_targets"], [])

    def test_verify_cannot_race_automatic_completion_worker(self):
        self.store.update_transaction(self.tx, "saved")
        self.store.update_session(self.session["id"], "saved", conversation_id=self.owner)
        with execution_lock(self.path), patch.object(self.service, "_components") as components:
            with self.assertRaises(ExecutionBusyError):
                self.service.verify_saved_draft(self.session["id"])
        components.assert_not_called()
        self.assertEqual(self.store.get_session(self.session["id"])["state"], "saved")

    def test_owner_change_while_process_checking_blocks_release(self):
        other = self.store.create_conversation()["conversation_id"]
        def transfer(_):
            self.store.transfer_session(self.session["id"], other, expected_owner=self.owner,
                                        expected_revision=0, expected_transaction_id=self.tx)
        self.process_check.side_effect = transfer
        with self.assertRaises(StateError):
            self.archive()
        self.assertEqual(self.store.get_session(self.session["id"])["conversation_id"], other)

    def test_execution_claim_racing_release_does_not_get_closed(self):
        self.process_check.side_effect = lambda _: self.store.claim_transaction(self.tx)
        with self.assertRaises(StateError):
            self.archive()
        self.assertEqual(self.store.get_transaction(self.tx)["state"], "executing")

    def test_restore_started_after_confirmation_snapshot_blocks_release(self):
        snapshot = self.store.get_session(self.session["id"], include_baseline=False)
        self.store.update_transaction(self.tx, "saved")
        self.store.update_session(self.session["id"], "verified", conversation_id=self.owner)
        restore = self.store.create_transaction(self.session["id"], None, "restore", "restore.json", conversation_id=self.owner)
        with self.assertRaises(StateError):
            self.archive(expected_snapshot=snapshot)
        self.assertEqual(self.store.get_session(self.session["id"])["transaction_id"], restore)

    def test_pending_restore_cancel_keeps_previous_pending_save_available(self):
        self.store.update_transaction(self.tx, "saved_state_pending")
        self.store.update_session(self.session["id"], "saved_state_pending", conversation_id=self.owner)
        path = self.root / "restore.json"
        save_artifact(path, {})
        restore = self.store.create_transaction(self.session["id"], None, "restore", str(path), conversation_id=self.owner)
        self.store.abort_session(self.session["id"], conversation_id=self.owner)
        current = self.store.get_session(self.session["id"])
        self.assertEqual((current["state"], current["transaction_id"]), ("saved_state_pending", self.tx))
        self.assertEqual(self.store.get_transaction(restore)["state"], "cancelled")

    def test_failed_restore_preserves_previous_pending_save(self):
        self.store.update_transaction(self.tx, "saved_state_pending")
        self.store.update_session(self.session["id"], "saved_state_pending", conversation_id=self.owner)
        restore = self.store.create_transaction(self.session["id"], None, "restore", str(self.root / "restore.json"), conversation_id=self.owner)
        self.store.claim_transaction(restore)
        self.store.release_failed_transaction(restore, "rolled_back", "rollback confirmed")
        current = self.store.get_session(self.session["id"])
        self.assertEqual((current["state"], current["transaction_id"]), ("saved_state_pending", self.tx))

    def test_human_declining_archive_or_stale_window_does_not_release(self):
        spec = importlib.util.spec_from_file_location("release_ui", PLUGIN_ROOT / "scripts" / "abort_session_app.py")
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        window = object.__new__(app.AbortSessionWindow)
        window.state, window.session_id = self.store, self.session["id"]
        window.conversation_id, window.owner_revision = self.owner, 0
        window.root = Mock()
        with patch.object(app.messagebox, "askyesno", return_value=False), patch("gxp_editor.recovery.archive_edit_session") as archive:
            window.archive()
            archive.assert_not_called()
        window.root.destroy.assert_not_called()
        other = self.store.create_conversation()["conversation_id"]
        self.store.transfer_session(self.session["id"], other, expected_owner=self.owner, expected_revision=0, expected_transaction_id=self.tx)
        with patch.object(app.messagebox, "showerror"), patch.object(app.messagebox, "askyesno") as confirm:
            window.archive()
            confirm.assert_not_called()


class ApprovalProcessCheckTests(unittest.TestCase):
    def test_legacy_approval_process_is_detected_without_lock_protocol(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "approval_app.py"
            script.write_text("import os,sys\nprint(os.getpid(), flush=True)\nsys.stdin.read()\n", encoding="utf-8")
            marker = "session-release-process-probe"
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            process = subprocess.Popen([sys.executable, str(script), marker], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **options)
            try:
                child_pid = int(process.stdout.readline().strip())
                self.assertIn(child_pid, approval_process_ids(marker))
                with self.assertRaises(ExecutionBusyError):
                    assert_approval_process_stopped(marker)
                self.assertEqual(approval_process_ids("different-session-release-marker"), [])
                os.kill(child_pid, signal.SIGTERM)
                process.wait(timeout=5)
                assert_approval_process_stopped(marker)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    def test_failed_process_enumeration_blocks_release(self):
        with patch("gxp_editor.process_check.approval_process_ids", side_effect=subprocess.TimeoutExpired("ps", 5)):
            with self.assertRaises(ExecutionBusyError):
                assert_approval_process_stopped("legacy-transaction")


if __name__ == "__main__":
    unittest.main()
