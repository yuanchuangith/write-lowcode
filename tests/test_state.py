from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

from tests.helpers import baseline
from gxp_editor.state import StateError, StateStore, saved_verification
from gxp_editor.approval import save_artifact


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = StateStore(Path(self.temporary.name) / "state.sqlite3")

    def test_only_one_cross_process_session_can_be_active(self):
        first = self.store.create_session(baseline(), "backup-1", "compiler-1")
        with self.assertRaisesRegex(StateError, "Another edit session is active"):
            self.store.create_session(baseline(id="design-2", ref_id="ref-2"), "backup-2", "compiler-1")
        self.store.update_session(first["id"], "aborted")
        second = self.store.create_session(baseline(id="design-2", ref_id="ref-2"), "backup-2", "compiler-1")
        self.assertEqual(second["ref_id"], "ref-2")

    def test_new_plan_supersedes_previous_ready_plan(self):
        session = self.store.create_session(baseline(), "backup-1", "compiler-1")
        first = self.store.create_plan(session["id"], "one.json", "old", "new-1", "compiler-1")
        second = self.store.create_plan(session["id"], "two.json", "old", "new-2", "compiler-1")
        self.assertEqual(self.store.get_plan(first)["status"], "superseded")
        self.assertEqual(self.store.get_plan(second)["status"], "ready")
        self.assertEqual(self.store.get_session(session["id"])["plan_id"], second)

    def test_blocked_plan_atomically_supersedes_ready_plan_and_can_be_replaced(self):
        session = self.store.create_session(baseline(), "backup-1", "compiler-1")
        first = self.store.create_plan(session["id"], "one.json", "old", "new-1", "compiler-1", expected_plan_id=None)
        blocked = self.store.create_plan(session["id"], "blocked.json", "old", "new-2", "compiler-1",
                                         initial_status="blocked", expected_plan_id=first)
        self.assertEqual(self.store.get_plan(first)["status"], "superseded")
        self.assertEqual(self.store.get_plan(blocked)["status"], "blocked")
        self.assertEqual(self.store.get_session(session["id"])["state"], "planned")
        with self.assertRaisesRegex(StateError, "Plan is no longer ready"):
            self.store.create_transaction(session["id"], blocked, "write", "tx.json")
        self.assertEqual(self.store.session_transactions(session["id"]), [])
        self.assertEqual(self.store.get_session(session["id"])["plan_id"], blocked)
        fixed = self.store.create_plan(session["id"], "fixed.json", "old", "new-3", "compiler-1", expected_plan_id=blocked)
        self.assertEqual(self.store.get_plan(blocked)["status"], "superseded")
        self.assertEqual(self.store.get_plan(fixed)["status"], "ready")

    def test_late_plan_cannot_overwrite_a_newer_blocked_result(self):
        session = self.store.create_session(baseline(), "backup-1", "compiler-1")
        ready = self.store.create_plan(session["id"], "one.json", "old", "new-1", "compiler-1")
        blocked = self.store.create_plan(session["id"], "blocked.json", "old", "new-2", "compiler-1",
                                         initial_status="blocked", expected_plan_id=ready)
        with self.assertRaisesRegex(StateError, "Current plan changed"):
            self.store.create_plan(session["id"], "late.json", "old", "late", "compiler-1", expected_plan_id=ready)
        self.assertEqual(self.store.get_session(session["id"])["plan_id"], blocked)
        self.assertEqual(self.store.get_plan(blocked)["status"], "blocked")

    def test_abort_cancels_blocked_plan(self):
        session = self.store.create_session(baseline(), "backup-1", "compiler-1")
        blocked = self.store.create_plan(session["id"], "blocked.json", "old", "new", "compiler-1", initial_status="blocked")
        self.store.abort_session(session["id"])
        self.assertEqual(self.store.get_plan(blocked)["status"], "cancelled")

    def test_two_concurrent_first_plans_only_allow_one_compare_and_swap(self):
        session = self.store.create_session(baseline(), "backup-1", "compiler-1")
        barrier = Barrier(2)

        def plan(index):
            barrier.wait(timeout=5)
            try:
                return self.store.create_plan(session["id"], f"{index}.json", "old", str(index), "compiler-1", expected_plan_id=None)
            except StateError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(plan, (1, 2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertIn(self.store.get_session(session["id"])["plan_id"], results)

    def test_session_rejects_second_active_approval_transaction(self):
        session = self.store.create_session(baseline(), "backup-1", "compiler-1")
        plan = self.store.create_plan(session["id"], "one.json", "old", "new", "compiler-1")
        self.store.create_transaction(session["id"], plan, "write", "tx-one.json", transaction_id="tx-one")
        with self.assertRaisesRegex(StateError, "active approval transaction"):
            self.store.create_transaction(session["id"], plan, "write", "tx-two.json", transaction_id="tx-two")

    def pending_transaction(self, **artifact_fields):
        session = self.store.create_session(baseline(), "backup-1", "compiler-1")
        plan = self.store.create_plan(session["id"], "one.json", "old", "new", "compiler-1")
        path = Path(self.temporary.name) / "tx.json"
        save_artifact(path, artifact_fields)
        tx = self.store.create_transaction(session["id"], plan, "write", str(path))
        return session["id"], plan, tx

    def test_manual_abort_cancels_pending_approval_and_allows_new_session(self):
        session, plan, tx = self.pending_transaction()
        self.store.abort_session(session)
        self.assertIsNone(self.store.active_session())
        self.assertEqual(self.store.get_transaction(tx)["state"], "cancelled")
        self.assertEqual(self.store.get_plan(plan)["status"], "cancelled")
        self.assertEqual(self.store.get_session(session)["backup_id"], "backup-1")
        with self.assertRaises(StateError):
            StateStore(self.store.path).claim_transaction(tx)
        self.store.create_session(baseline(id="design-2", ref_id="ref-2"), "backup-2", "compiler-1")

    def test_verified_save_releases_session_and_retains_evidence_atomically(self):
        session, _, tx = self.pending_transaction()
        self.store.update_transaction(tx, "saved")
        self.store.update_session(session, "saved")
        receipt = self.store.finish_verified_save(
            tx, content_hash="approved", relation_hash="relations", conversation_id="", owner_revision=0,
        )
        self.assertIsNone(self.store.active_session())
        self.assertEqual(saved_verification(self.store.get_session(session), self.store.get_transaction(tx)), receipt)
        self.assertEqual(self.store.get_session(session)["backup_id"], "backup-1")

    def test_verified_save_cannot_release_a_new_restore_or_stale_owner(self):
        session, _, tx = self.pending_transaction()
        self.store.update_transaction(tx, "saved")
        self.store.update_session(session, "saved")
        args = {"content_hash": "approved", "relation_hash": "relations", "conversation_id": "", "owner_revision": 0}
        with self.assertRaises(StateError):
            self.store.finish_verified_save(tx, **{**args, "owner_revision": 1})
        restore = self.store.create_transaction(session, None, "restore", "restore.json")
        with self.assertRaisesRegex(StateError, "Session changed"):
            self.store.finish_verified_save(tx, **args)
        self.assertEqual(self.store.get_session(session)["transaction_id"], restore)
        self.assertEqual(self.store.get_transaction(tx)["message"], "")

    def test_uncertain_transaction_cannot_record_successful_verification(self):
        session, _, tx = self.pending_transaction()
        for state in ("awaiting_approval", "executing", "saved_state_pending", "failed"):
            with self.subTest(state=state):
                self.store.update_transaction(tx, state)
                with self.assertRaisesRegex(StateError, "committed save"):
                    self.store.finish_verified_save(
                        tx, content_hash="approved", relation_hash="relations", conversation_id="", owner_revision=0,
                    )
                self.assertEqual(self.store.get_session(session)["state"], "awaiting_approval")

    def test_local_failure_rolls_back_both_receipt_and_release(self):
        session, _, tx = self.pending_transaction()
        self.store.update_transaction(tx, "saved")
        self.store.update_session(session, "saved")
        with self.store._connection() as db:
            db.execute("CREATE TRIGGER block_finish BEFORE UPDATE OF state ON sessions "
                       "WHEN NEW.state='finished' BEGIN SELECT RAISE(ABORT, 'test completion failure'); END")
        with self.assertRaisesRegex(Exception, "test completion failure"):
            self.store.finish_verified_save(
                tx, content_hash="approved", relation_hash="relations", conversation_id="", owner_revision=0,
            )
        self.assertEqual(self.store.get_session(session)["state"], "saved")
        self.assertEqual(self.store.get_transaction(tx)["message"], "")

    def test_execution_claim_wins_over_abort(self):
        session, _, tx = self.pending_transaction()
        StateStore(self.store.path).claim_transaction(tx)
        with self.assertRaisesRegex(StateError, "Execution has been attempted"):
            self.store.abort_session(session)
        self.assertEqual(self.store.active_session()["id"], session)
        self.assertEqual(self.store.get_transaction(tx)["state"], "executing")

    def test_simultaneous_abort_and_execute_only_one_can_win(self):
        session, _, tx = self.pending_transaction()
        barrier = Barrier(2)

        def compete(operation):
            store = StateStore(self.store.path)
            barrier.wait(timeout=5)
            try:
                store.abort_session(session) if operation == "abort" else store.claim_transaction(tx)
                return operation
            except StateError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, ("abort", "execute")))
        self.assertEqual(sum(result is not None for result in results), 1)
        winner = next(result for result in results if result)
        self.assertEqual(self.store.get_transaction(tx)["state"], "cancelled" if winner == "abort" else "executing")

    def test_legacy_attempt_or_saved_state_cannot_be_cleared(self):
        for flag in ("execution_started", "executed"):
            with self.subTest(flag=flag):
                store = StateStore(Path(self.temporary.name) / f"{flag}.sqlite3")
                session = store.create_session(baseline(), "backup", "compiler")
                plan = store.create_plan(session["id"], "plan.json", "old", "new", "compiler")
                path = Path(self.temporary.name) / f"{flag}.json"
                save_artifact(path, {flag: True})
                store.create_transaction(session["id"], plan, "write", str(path))
                with self.assertRaisesRegex(StateError, "already started"):
                    store.abort_session(session["id"])
                self.assertIsNotNone(store.active_session())

    def test_failed_and_uncertain_results_are_never_cleared(self):
        session, _, tx = self.pending_transaction()
        for state in ("failed", "executing", "saved", "saved_state_pending"):
            self.store.update_transaction(tx, state)
            with self.subTest(state=state), self.assertRaises(StateError):
                self.store.abort_session(session)
        self.assertEqual(self.store.active_session()["id"], session)

    def test_legacy_pre_write_rejection_can_be_aborted_without_deleting_history(self):
        session, plan, tx = self.pending_transaction(execution_started=True)
        message = "The draft content changed after planning; transaction was not executed"
        self.store.update_transaction(tx, "failed", message)
        self.store.abort_session(session)
        self.assertIsNone(self.store.active_session())
        self.assertEqual(self.store.get_transaction(tx)["state"], "failed_no_write")
        self.assertEqual(self.store.get_transaction(tx)["message"], message)
        self.assertEqual(self.store.get_plan(plan)["status"], "cancelled")

    def test_confirmed_failures_release_lock_and_cannot_be_reexecuted(self):
        for outcome in ("failed_no_write", "rolled_back"):
            with self.subTest(outcome=outcome):
                session, plan, tx = self.pending_transaction(execution_started=True)
                self.store.claim_transaction(tx)
                self.store.release_failed_transaction(tx, outcome, "confirmed failure")
                self.assertIsNone(self.store.active_session())
                self.assertEqual(self.store.get_plan(plan)["status"], "cancelled")
                self.assertEqual(self.store.get_session(session)["state"], outcome)
                with self.assertRaises(StateError):
                    self.store.claim_transaction(tx)

    def test_unknown_failure_cannot_be_reported_as_confirmed(self):
        session, _, tx = self.pending_transaction()
        self.store.claim_transaction(tx)
        with self.assertRaises(StateError):
            self.store.release_failed_transaction(tx, "failed", "unknown")
        self.assertEqual(self.store.active_session()["id"], session)

    def test_aborted_session_cannot_create_plan_or_approval(self):
        session = self.store.create_session(baseline(), "backup", "compiler")
        plan = self.store.create_plan(session["id"], "plan.json", "old", "new", "compiler")
        self.store.abort_session(session["id"])
        with self.assertRaises(StateError):
            self.store.create_plan(session["id"], "new.json", "old", "new", "compiler")
        with self.assertRaises(StateError):
            self.store.create_transaction(session["id"], plan, "write", "tx.json")


if __name__ == "__main__":
    unittest.main()
