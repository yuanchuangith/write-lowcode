"""Session completion and restore guards with temporary local state only."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from threading import Barrier
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.helpers import baseline, editor_config, runtime_validation
from gxp_editor.hashing import design_content_hash, relation_snapshot_hash
from gxp_editor.service import EditorService, EditorServiceError
from gxp_editor.state import StateError, StateStore


class _SessionFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = StateStore(self.root / "state.sqlite3")
        self.owner = self.store.create_conversation("synthetic completion")["conversation_id"]
        self.session = self.store.create_session(baseline(csharp_code=""), "backup", "compiler",
                                                 conversation_id=self.owner)
        self.service = object.__new__(EditorService)
        self.service.state = self.store
        self.service._conversation_id = self.owner
        self.enterContext(patch("gxp_editor.service.assert_session_database_scope"))

    def finish(self):
        return self.store.finish_no_changes(self.session["id"], conversation_id=self.owner, owner_revision=0)


class SessionCompletionTests(_SessionFixture):
    def test_no_changes_releases_fresh_session_and_allows_another_target(self):
        result = self.service._no_changes_response(self.session)
        self.assertTrue(result["no_changes"])
        self.assertTrue(result["session_released"])
        self.assertFalse(result["write_executed"])
        self.assertIsNone(self.store.active_session(self.owner))
        self.assertEqual(self.store.get_session(self.session["id"])["backup_id"], "backup")
        repeated = self.service._no_changes_response(self.session)
        self.assertTrue(repeated["session_released"])
        self.assertEqual(repeated["next_action"], "report_no_changes")
        self.store.create_session(baseline(id="other", ref_id="other-ref"), "next-backup", "compiler",
                                  conversation_id=self.owner)

    def test_no_changes_preserves_existing_plan_even_with_stale_input(self):
        plan = self.store.create_plan(self.session["id"], "plan.json", "old", "new", "compiler",
                                      conversation_id=self.owner)
        result = self.service._no_changes_response(self.session)
        self.assertFalse(result["session_released"])
        self.assertEqual(result["existing_plan_id"], plan)
        self.assertEqual(self.store.get_plan(plan)["status"], "ready")
        self.assertEqual(result["next_read"]["arguments"]["conversation_id"], self.owner)

    def test_no_changes_does_not_release_stale_owner_or_historical_plan(self):
        with self.assertRaises(StateError):
            self.store.finish_no_changes(self.session["id"], conversation_id=self.owner, owner_revision=1)
        self.store.create_plan(self.session["id"], "plan.json", "old", "new", "compiler",
                                conversation_id=self.owner)
        self.store.update_session(self.session["id"], "locked", conversation_id=self.owner, plan_id=None)
        self.assertFalse(self.finish())

    def test_racing_plan_and_no_changes_cannot_discard_work(self):
        barrier = Barrier(2)

        def plan():
            barrier.wait(timeout=5)
            try:
                return self.store.create_plan(self.session["id"], "plan.json", "old", "new", "compiler",
                                               conversation_id=self.owner)
            except StateError:
                return None

        def finish():
            barrier.wait(timeout=5)
            return self.finish()

        with ThreadPoolExecutor(max_workers=2) as pool:
            planned = pool.submit(plan)
            completed = pool.submit(finish)
            plan_id, released = planned.result(timeout=10), completed.result(timeout=10)
        self.assertNotEqual(plan_id is not None, released)
        latest = self.store.get_session(self.session["id"])
        self.assertEqual(latest["state"], "finished" if released else "planned")
        self.assertEqual(latest["plan_id"], plan_id)


class FinishedRestoreTests(_SessionFixture):
    def setUp(self):
        super().setUp()
        self.current = baseline(js_code="saved-js", csharp_code="")
        self.plan = self.store.create_plan(self.session["id"], "plan.json", "old", "new", "compiler",
                                          conversation_id=self.owner)
        self.tx = self.store.create_transaction(self.session["id"], self.plan, "write", "old.json",
                                                conversation_id=self.owner)
        self.store.update_transaction(self.tx, "saved")
        self.store.update_session(self.session["id"], "saved", conversation_id=self.owner)
        self.receipt = self.store.finish_verified_save(self.tx, content_hash=design_content_hash(self.current),
            relation_hash=relation_snapshot_hash([]), conversation_id=self.owner, owner_revision=0)
        self.session = self.store.get_session(self.session["id"])
        self.repository = Mock()
        self.repository.database.read_session.side_effect = lambda: nullcontext(object())
        self.repository.load_draft_by_id.side_effect = lambda *args, **kwargs: dict(self.current)
        self.repository._relations.return_value = []
        self.enterContext(patch.object(self.service, "_components", return_value=(
            editor_config(), object(), self.repository, object(), object())))
        self.enterContext(patch.object(self.service, "editor_status", return_value={"write_ready": True}))
        self.enterContext(patch.object(self.service, "_database_safety_binding", return_value={
            "account_mode": "dedicated", "grants_sha256": "c" * 64, "shared_account": False,
            "privileged_shared_override": False}))
        self.enterContext(patch.object(self.service, "_runtime_validation_evidence", return_value=runtime_validation("")))
        self.enterContext(patch("gxp_editor.service.approval_secret", return_value="synthetic-key"))
        self.backup = self.enterContext(patch("gxp_editor.service.BackupStore"))
        self.backup.return_value.load.return_value = {
            "manifest": {"design_id": self.session["design_id"]},
            "design.json": baseline(csharp_code=""), "relations.json": []}
        self.enterContext(patch("gxp_editor.service.runtime_root", return_value=self.root))
        self.launch = self.enterContext(patch.object(self.service, "_try_launch_approval", return_value={
            "approval_window_opened": True}))

    def restore(self, **kwargs):
        return self.store.create_transaction(self.session["id"], None, "restore", "restore.json",
            conversation_id=self.owner, owner_revision=0, expected_finished_save=self.receipt, **kwargs)

    def test_finished_save_can_prepare_fresh_human_restore_approval(self):
        result = self.service.prepare_restore(self.session["id"], "backup")
        self.assertTrue(result["approval_window_opened"])
        self.assertNotEqual(result["transaction_id"], self.tx)
        self.assertEqual(self.store.get_transaction(result["transaction_id"])["state"], "awaiting_approval")
        self.assertEqual(self.store.get_session(self.session["id"])["state"], "restore_pending")
        self.assertEqual(self.store.get_transaction(self.tx)["state"], "saved")
        self.repository.load_draft_by_id.assert_called_once()
        self.assertIn("session", self.repository.load_draft_by_id.call_args.kwargs)

    def test_finished_restore_refuses_content_relation_identity_or_status_drift(self):
        for change in ({"js_code": "later-js"}, {"id": "other"}, {"ref_id": "other-ref"},
                       {"is_publish": 1}, {"isDeleted": 1}, {"is_publish": None}):
            with self.subTest(change=change), patch.dict(self.current, change):
                with self.assertRaises(EditorServiceError):
                    self.service.prepare_restore(self.session["id"], "backup")
        self.repository._relations.return_value = [{"id": "new-relation"}]
        with self.assertRaises(EditorServiceError):
            self.service.prepare_restore(self.session["id"], "backup")
        self.launch.assert_not_called()
        self.assertEqual(len(self.store.session_transactions(self.session["id"])), 1)

    def test_finished_restore_requires_receipt_and_atomic_exclusive_occupancy(self):
        with self.assertRaisesRegex(StateError, "verified save receipt"):
            self.store.create_transaction(self.session["id"], None, "restore", "restore.json",
                                          conversation_id=self.owner)
        other = self.store.create_session(baseline(id="other", ref_id="other-ref"), "backup2", "compiler",
                                            conversation_id=self.owner)
        with self.assertRaisesRegex(StateError, "Another edit session"):
            self.restore()
        self.store.finish_no_changes(other["id"], conversation_id=self.owner, owner_revision=0)
        self.assertTrue(self.restore())

    def test_finished_restore_rejects_later_saved_edit_history(self):
        later = self.store.create_session(self.current, "new-backup", "compiler", conversation_id=self.owner)
        plan = self.store.create_plan(later["id"], "later-plan.json", "a", "b", "compiler",
                                       conversation_id=self.owner)
        tx = self.store.create_transaction(later["id"], plan, "write", "later.json", conversation_id=self.owner)
        self.store.update_transaction(tx, "saved")
        self.store.update_session(later["id"], "saved", conversation_id=self.owner)
        self.store.finish_verified_save(tx, content_hash=self.receipt["content_hash"],
            relation_hash=self.receipt["relation_hash"], conversation_id=self.owner, owner_revision=0)
        with self.assertRaisesRegex(StateError, "later saved editing session"):
            self.restore()

    def test_cancelling_restore_retains_prior_save_and_can_finish(self):
        self.restore()
        self.store.abort_session(self.session["id"], conversation_id=self.owner)
        session = self.store.get_session(self.session["id"])
        self.assertEqual(session["transaction_id"], self.tx)
        self.assertEqual(session["state"], "saved")
        self.store.finish_verified_save(self.tx, content_hash=self.receipt["content_hash"],
            relation_hash=self.receipt["relation_hash"], conversation_id=self.owner, owner_revision=0)
        self.assertIsNone(self.store.active_session(self.owner))
