"""Commit attribution stays separate from matching synthetic draft content."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tests import test_recovery as recovery_fixtures
from tests.helpers import editor_config
from gxp_editor.approval import save_artifact
from gxp_editor.execution_evidence import execution_evidence
from gxp_editor.service import EditorService
from gxp_editor.state import StateError, saved_verification


class ExecutionEvidenceTests(unittest.TestCase):
    def test_saved_state_and_historical_scope_without_attribution_do_not_prove_execution(self):
        for message in ("", json.dumps({"saved_verification": {"scope": "committed_draft_snapshot"}})):
            result = execution_evidence({"state": "saved", "message": message})
            self.assertFalse(result["executed"])
            self.assertEqual(result["execution_outcome"], "unknown")
            self.assertFalse(result["original_transaction_retry_allowed"])

    def test_complete_executor_statement_receipt_proves_execution(self):
        artifact = {"executed": True, "statements": [{"statement_hash": "a" * 64}],
                    "executed_statement_hashes": ["a" * 64]}
        result = execution_evidence({"state": "saved"}, artifact)
        self.assertTrue(result["executed"])
        self.assertEqual(result["commit_evidence"], "executor_commit_receipt")

    def test_incomplete_or_mismatched_statement_receipt_does_not_prove_execution(self):
        for hashes in ([None], [], ["other"]):
            artifact = {"executed": True, "statements": [{"statement_hash": "a" * 64}],
                        "executed_statement_hashes": hashes}
            self.assertEqual(execution_evidence({"state": "saved"}, artifact)["execution_outcome"], "unknown")

    def test_explicit_unknown_observation_cannot_be_upgraded_by_saved_or_artifact_flags(self):
        observation = {"schema_version": 1, "execution_outcome": "unknown",
                       "commit_evidence": "snapshot_match_only", "verification_scope": "current_draft_snapshot"}
        artifact = {"executed": True, "statements": [{"statement_hash": "a" * 64}],
                    "executed_statement_hashes": ["a" * 64]}
        result = execution_evidence({"state": "saved"}, artifact, observation)
        self.assertFalse(result["executed"])
        self.assertEqual(result["commit_evidence"], "snapshot_match_only")

    def test_pending_without_execution_markers_is_not_started(self):
        self.assertEqual(execution_evidence({"state": "awaiting_approval"}, {})["execution_outcome"], "not_started")
        self.assertEqual(execution_evidence({"state": "awaiting_approval"}, {"execution_started": True})["execution_outcome"], "unknown")


class RecoveryAttributionTests(unittest.TestCase):
    setUp = recovery_fixtures.RecoveryTests.setUp
    recover = recovery_fixtures.RecoveryTests.recover
    snapshot = recovery_fixtures.RecoveryTests.snapshot

    def service(self):
        service = object.__new__(EditorService)
        service.state = self.store
        service._conversation_id = ""
        return service

    def test_lost_ack_matching_candidate_stays_unknown_through_the_complete_history_chain(self):
        self.store.update_transaction(self.tx, "failed", "synthetic commit acknowledgement lost")
        recovered = self.recover()
        self.assertTrue(recovered["verified"])
        self.assertTrue(recovered["current_matches_candidate"])
        self.assertFalse(recovered["executed"])
        self.assertEqual(recovered["execution_outcome"], "unknown")
        observation = self.store.execution_observation(self.tx)
        self.assertFalse(observation["executed"])
        self.assertEqual(observation["verification_scope"], "current_draft_snapshot")
        service = self.service()
        with patch.object(service, "_components", return_value=(editor_config(), None, self.repo, None, None)):
            status = service.get_write_status(self.tx)
            self.assertFalse(status["executed"])
            self.assertEqual(status["execution_outcome"], "unknown")
            self.assertFalse(status["current_draft_rechecked"])
            self.assertIsNone(status["current_matches_candidate"])
            verified = service.verify_saved_draft(self.session["id"])
            self.assertTrue(verified["verified"])
            self.assertTrue(verified["current_draft_rechecked"])
            self.assertEqual(verified["execution_outcome"], "unknown")
            finished = service.finish_edit_session(self.session["id"])
            self.assertTrue(finished["finished"])
            self.assertFalse(finished["executed"])
        # A new service/StateStore and a missing artifact must not upgrade the
        # independent persisted attribution when revisiting the historical save.
        from gxp_editor.state import StateStore
        service.state = StateStore(self.store.path)
        self.path.unlink()
        with patch.object(service, "_components", side_effect=AssertionError("No live historical read")):
            for result in (service.get_write_status(self.tx), service.verify_saved_draft(self.session["id"]),
                           service.finish_edit_session(self.session["id"]), service.get_write_status(self.tx)):
                self.assertFalse(result["executed"])
                self.assertEqual(result["execution_outcome"], "unknown")
                self.assertFalse(result["current_draft_rechecked"])
        receipt = saved_verification(service.state.get_session(self.session["id"]), service.state.get_transaction(self.tx))
        self.assertEqual(receipt["scope"], "current_draft_snapshot")
        self.assertEqual(receipt["execution_outcome"], "unknown")
        with self.assertRaises(StateError):
            service.state.claim_transaction(self.tx)
        self.database_session.execute.assert_not_called()

    def test_baseline_match_does_not_prove_transaction_never_executed(self):
        self.repo.load_draft_by_id.return_value = self.snapshot(self.artifact["old_content_hash"])
        result = self.recover()
        self.assertEqual(result["outcome"], "not_applied")
        self.assertTrue(result["current_matches_baseline"])
        self.assertEqual(result["execution_outcome"], "unknown")
        self.assertFalse(result["original_transaction_retry_allowed"])
        self.assertEqual(self.store.execution_observation(self.tx)["execution_outcome"], "unknown")

    def test_saved_then_reverted_remains_a_later_change(self):
        self.store.update_transaction(self.tx, "saved")
        self.repo.load_draft_by_id.return_value = self.snapshot(self.artifact["old_content_hash"])
        result = self.recover()
        self.assertEqual(result["outcome"], "changed")
        self.assertTrue(result["current_matches_baseline"])
        self.assertFalse(result["current_matches_candidate"])
        self.assertEqual(self.store.execution_observation(self.tx)["execution_outcome"], "unknown")

    def test_confirmed_executor_receipt_is_retained_when_later_data_changes(self):
        self.artifact["executed"] = True
        self.artifact["executed_statement_hashes"] = [item["statement_hash"] for item in self.artifact["statements"]]
        save_artifact(self.path, self.artifact)
        self.store.update_transaction(self.tx, "saved")
        self.repo.load_draft_by_id.return_value = self.snapshot("external-change")
        result = self.recover()
        self.assertEqual(result["outcome"], "changed")
        self.assertTrue(result["executed"])
        self.assertEqual(result["commit_evidence"], "executor_commit_receipt")
        self.assertFalse(result["current_matches_candidate"])
        self.assertTrue(self.store.execution_observation(self.tx)["executed"])

    def test_corrupt_baseline_does_not_block_artifact_bound_recovery(self):
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET baseline_json='corrupt-json' WHERE id=?", (self.session["id"],))
        result = self.recover()
        self.assertEqual(result["outcome"], "saved")
        self.assertEqual(result["execution_outcome"], "unknown")

    def test_finishing_snapshot_verification_does_not_upgrade_unknown_to_commit_confirmation(self):
        self.recover()
        receipt = self.store.finish_verified_save(self.tx, content_hash=self.artifact["new_content_hash"],
            relation_hash=self.artifact["expected_relation_hash"], conversation_id="", owner_revision=0)
        self.assertFalse(receipt["executed"])
        self.assertEqual(receipt["scope"], "current_draft_snapshot")
        self.assertEqual(self.store.execution_observation(self.tx)["execution_outcome"], "unknown")

    def test_matching_content_with_published_or_deleted_target_is_not_verified(self):
        for key in ("is_publish", "isDeleted"):
            with self.subTest(key=key):
                self.repo.load_draft_by_id.return_value = {**self.snapshot(self.artifact["new_content_hash"]), key: 1}
                result = self.recover()
                self.assertFalse(result["verified"])
                self.assertEqual(result["outcome"], "changed")
                self.assertEqual(result["target_state"], "changed")

    def test_matching_hash_without_current_target_state_is_unknown(self):
        self.repo.load_draft_by_id.return_value = {"content_hash": self.artifact["new_content_hash"]}
        result = self.recover()
        self.assertFalse(result["verified"])
        self.assertEqual(result["outcome"], "unknown")
        self.assertIsNone(result["current_matches_candidate"])
        self.assertEqual(self.store.get_transaction(self.tx)["state"], "executing")


if __name__ == "__main__":
    unittest.main()
