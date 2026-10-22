"""Broken review/startup paths must preserve an actionable, read-only exit."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import baseline, transaction
from gxp_editor.approval import save_artifact, transaction_binding_hash
from gxp_editor.execution_lock import ExecutionBusyError
from gxp_editor.service import EditorService
from gxp_editor.state import StateStore


class RecoveryServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = StateStore(self.root / "state.sqlite3")
        self.session = self.store.create_session(baseline(), "backup", "compiler")
        plan = self.store.create_plan(self.session["id"], "plan.json", "old", "new", "compiler")
        self.artifact = transaction()
        self.artifact.update(session_id=self.session["id"], plan_id=plan, transaction_id="recovery-tx")
        self.artifact["transaction_hash"] = transaction_binding_hash(self.artifact)
        self.path = self.root / "approval.json"
        save_artifact(self.path, self.artifact)
        self.tx = self.store.create_transaction(self.session["id"], plan, "write", str(self.path), transaction_id="recovery-tx")
        self.service = object.__new__(EditorService)
        self.service.state = self.store
        self.enterContext(patch.object(self.service, "_components", side_effect=AssertionError("No business connection")))
        self.startup = self.enterContext(patch("gxp_editor.ui_launch.inspect_window_startup", return_value={
            "window_opened": False, "window_starting": False, "startup_status": "not_started"}))
        self.launch = self.enterContext(patch.object(self.service, "_launch_approval", return_value={
            "window_opened": True, "window_starting": False, "startup_status": "ready"}))

    def test_broken_approval_returns_recovery_options_without_mutation(self):
        for content in (None, "bad-json", "[]", '{"statements":3,"approvals":7}'):
            with self.subTest(content=content):
                if content is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.write_text(content, encoding="utf-8")
                result = self.service.reopen_approval(self.tx)
                self.assertFalse(result["approval_window_opened"])
                self.assertEqual(result["reason_code"], "approval_artifact_unavailable")
                self.assertEqual(result["handling_options"]["local_release_tool"], "release_local_session")
                self.assertEqual(self.store.get_transaction(self.tx)["state"], "awaiting_approval")
                status = self.service.get_write_status(self.tx)
                self.assertFalse(status["artifact_valid"])
                self.assertTrue(status["handling_available"])
                self.assertIn("window_startup", status)
        self.launch.assert_not_called()

    def test_old_or_attempted_approval_is_not_reopened(self):
        self.store.update_transaction(self.tx, "executing")
        result = self.service.reopen_approval(self.tx)
        self.assertEqual(result["reason_code"], "approval_not_pending")
        self.store.update_transaction(self.tx, "awaiting_approval")
        self.artifact["execution_started"] = True
        save_artifact(self.path, self.artifact)
        result = self.service.reopen_approval(self.tx)
        self.assertEqual(result["reason_code"], "execution_result_uncertain")
        self.launch.assert_not_called()

    def test_corrupt_baseline_does_not_block_valid_pending_reopen(self):
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET baseline_json='bad-json' WHERE id=?", (self.session["id"],))
        with patch.object(self.service, "_launch_approval", wraps=EditorService._launch_approval.__get__(self.service)), patch(
                "gxp_editor.ui_launch.launch_window", return_value={"window_opened": True, "window_starting": False}):
            self.assertTrue(self.service.reopen_approval(self.tx)["approval_window_opened"])

    def test_pending_startup_is_never_reported_as_an_open_window(self):
        self.launch.return_value = {"window_opened": False, "window_starting": True, "startup_status": "starting"}
        result = self.service.reopen_approval(self.tx)
        self.assertFalse(result["approval_window_opened"])
        self.assertTrue(result["approval_window_starting"])
        self.assertEqual(result["window_startup"]["startup_status"], "starting")

    def test_missing_receipt_is_not_success(self):
        self.launch.return_value = None
        result = self.service.reopen_approval(self.tx)
        self.assertFalse(result["approval_window_opened"])
        self.assertIn("回执", result["approval_window_error"])

    def test_explicit_reopen_recovers_only_freshly_verified_unexecuted_window(self):
        self.startup.return_value = {"startup_status": "heartbeat_timeout", "startup_id": "fixture-startup"}
        def recover(key, startup_id, *, verify_unexecuted):
            self.assertEqual(startup_id, "fixture-startup")
            self.assertTrue(verify_unexecuted(self.path))
            self.assertFalse(verify_unexecuted(self.root / "another.json"))
            self.artifact["execution_started"] = True
            save_artifact(self.path, self.artifact)
            self.assertFalse(verify_unexecuted(self.path))
            return {"window_recovered": True, "write_executed": False, "approval_granted": False}
        with patch("gxp_editor.ui_launch.recover_stalled_window", side_effect=recover) as recovery:
            result = self.service.reopen_approval(self.tx)
        recovery.assert_called_once()
        self.assertTrue(result["approval_window_opened"])
        self.assertFalse(result["window_recovery"]["approval_granted"])
        self.assertEqual(self.store.get_transaction(self.tx)["state"], "awaiting_approval")

    def test_refused_stalled_recovery_never_launches_duplicate_window(self):
        self.startup.return_value = {"startup_status": "startup_timeout", "startup_id": "fixture-startup"}
        with patch("gxp_editor.ui_launch.recover_stalled_window", return_value={
                "window_recovered": False, "window_error": "executor is busy"}):
            result = self.service.reopen_approval(self.tx)
        self.assertEqual(result["reason_code"], "stalled_window_recovery_refused")
        self.assertFalse(result["approval_window_opened"])
        self.launch.assert_not_called()

    def test_recovery_callback_rejects_transfer_while_reopening(self):
        self.startup.return_value = {"startup_status": "heartbeat_timeout", "startup_id": "fixture-startup"}
        def recover(key, startup_id, *, verify_unexecuted):
            with self.store._connection() as db:
                db.execute("UPDATE sessions SET owner_revision=1 WHERE id=?", (self.session["id"],))
            self.assertFalse(verify_unexecuted(self.path))
            return {"window_recovered": False, "window_error": "ownership changed"}
        with patch("gxp_editor.ui_launch.recover_stalled_window", side_effect=recover):
            result = self.service.reopen_approval(self.tx)
        self.assertEqual(result["reason_code"], "stalled_window_recovery_refused")
        self.launch.assert_not_called()

    def test_recovery_errors_return_options_without_releasing_or_claiming_success(self):
        for error in (ValueError("bad legacy artifact"), ExecutionBusyError("executor active")):
            with self.subTest(error=type(error).__name__), patch("gxp_editor.service.recover_session", side_effect=error):
                result = self.service.recover_edit_session(self.session["id"])
                self.assertEqual(result["outcome"], "recovery_blocked")
                self.assertFalse(result["verified"])
                self.assertFalse(result["session_released"])
                self.assertFalse(result["business_data_written"])
                self.assertEqual(result["handling_options"]["inspect_tool"], "inspect_session_cancellation")
        self.assertIsNotNone(self.store.active_session())

    def test_handling_startup_failure_keeps_chat_alternative(self):
        with patch("gxp_editor.ui_launch.launch_window", return_value={
                "window_opened": False, "window_starting": False, "startup_status": "failed", "window_error": "init failed"}):
            result = self.service.abort_edit_session(self.session["id"])
        self.assertFalse(result["confirmation_window_opened"])
        self.assertEqual(result["window_startup"]["window_error"], "init failed")
        self.assertEqual(result["handling_options"]["local_release_tool"], "release_local_session")
        self.assertIsNotNone(self.store.active_session())


if __name__ == "__main__":
    unittest.main()
