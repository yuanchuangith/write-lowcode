"""Synthetic queue handoff races use only a temporary SQLite state."""
import concurrent.futures
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.helpers import baseline
from gxp_editor.queue_protocol import (queue_snapshot, reserve_launch, release_launch,
    claim_window, validate_window, TOKEN_ENV, GENERATION_ENV)
from gxp_editor.state import StateStore, StateError
from gxp_editor.hashing import sha256_text


class QueueProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = StateStore(Path(temporary.name) / "state.sqlite3")
        self.owner = self.state.create_conversation("fixture")["conversation_id"]
        self.queue = self.state.create_edit_queue(self.owner)
        self.session = self.state.create_session(baseline(), "backup", "compiler",
            conversation_id=self.owner, queue_id=self.queue)
        self.plan = self.state.create_plan(self.session["id"], "plan.json", "old", "new", "compiler",
            conversation_id=self.owner)
        self.tx = self.state.create_transaction(self.session["id"], self.plan, "write", "approval.json",
            conversation_id=self.owner)

    def view(self):
        return queue_snapshot(self.state, self.queue, self.owner)

    def exit_proof(self, reservation):
        return {"generation": reservation["generation"], "launch_token_sha256": sha256_text(reservation["launch_token"])}

    def test_concurrent_reopen_has_exactly_one_generation_reservation(self):
        view = self.view()
        def reserve():
            try:
                return reserve_launch(self.state, self.queue, self.owner, view)
            except StateError:
                return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: reserve(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.view()["generation"], 1)

    def test_membership_pointer_transfer_and_execution_invalidate_snapshot(self):
        for column, value in (("owner_revision", 1), ("state", "saved"), ("transaction_id", None)):
            with self.subTest(column=column):
                view = self.view()
                with self.state._connection() as db:
                    db.execute(f"UPDATE sessions SET {column}=? WHERE id=?", (value, self.session["id"]))
                with self.assertRaisesRegex(StateError, "changed during preflight"):
                    reserve_launch(self.state, self.queue, self.owner, view)

    def test_old_generation_cannot_claim_execute_or_release_new_generation(self):
        first = reserve_launch(self.state, self.queue, self.owner, self.view())
        with patch.dict(os.environ, {TOKEN_ENV: first["launch_token"], GENERATION_ENV: str(first["generation"])}):
            claim_window(self.state, self.queue, self.owner)
            validate_window(self.state, self.queue, self.owner)
        second = reserve_launch(self.state, self.queue, self.owner, self.view(), previous_exited=self.exit_proof(first))
        with patch.dict(os.environ, {TOKEN_ENV: first["launch_token"], GENERATION_ENV: str(first["generation"])}):
            with self.assertRaises(StateError):
                claim_window(self.state, self.queue, self.owner)
            with self.assertRaises(StateError):
                self.state.claim_transaction(self.tx)
        release_launch(self.state, first)
        self.assertEqual(self.view()["launch_state"], "reserved")
        with patch.dict(os.environ, {TOKEN_ENV: second["launch_token"], GENERATION_ENV: str(second["generation"])}):
            claim_window(self.state, self.queue, self.owner)
            self.state.claim_transaction(self.tx)
        self.assertEqual(self.state.get_transaction(self.tx)["state"], "executing")

    def test_uncertain_and_unresolved_reservation_never_retry_by_timeout(self):
        first = reserve_launch(self.state, self.queue, self.owner, self.view())
        with self.assertRaisesRegex(StateError, "already reserved"):
            reserve_launch(self.state, self.queue, self.owner, self.view())
        self.state.update_transaction(self.tx, "saved_state_pending")
        with self.assertRaisesRegex(StateError, "uncertain"):
            reserve_launch(self.state, self.queue, self.owner, self.view(), previous_exited=self.exit_proof(first))

    def test_new_member_is_not_covered_by_old_preflight(self):
        view = self.view()
        self.state.create_session(baseline(id="design-2", ref_id="ref-2"), "backup-2", "compiler",
            conversation_id=self.owner, queue_id=self.queue)
        with self.assertRaises(StateError):
            reserve_launch(self.state, self.queue, self.owner, view)

    def test_old_exited_attempt_cannot_replace_unlaunched_new_reservation(self):
        first = reserve_launch(self.state, self.queue, self.owner, self.view())
        proof = self.exit_proof(first)
        second = reserve_launch(self.state, self.queue, self.owner, self.view(), previous_exited=proof)
        with self.assertRaises(StateError):
            reserve_launch(self.state, self.queue, self.owner, self.view(), previous_exited=proof)
        self.assertEqual(self.view()["generation"], second["generation"])

    def test_child_rejects_membership_change_between_reserve_and_claim(self):
        reservation = reserve_launch(self.state, self.queue, self.owner, self.view())
        self.state.create_session(baseline(id="second", ref_id="second"), "backup", "compiler",
            conversation_id=self.owner, queue_id=self.queue)
        with patch.dict(os.environ, {TOKEN_ENV: reservation["launch_token"], GENERATION_ENV: str(reservation["generation"])}):
            with self.assertRaisesRegex(StateError, "after launch reservation"):
                claim_window(self.state, self.queue, self.owner)

    def test_busy_control_fails_without_waiting_on_other_writer(self):
        import time
        view = self.view()
        with self.state._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            started = time.perf_counter()
            with self.assertRaisesRegex(StateError, "busy"):
                reserve_launch(self.state, self.queue, self.owner, view)
            self.assertLess(time.perf_counter() - started, 0.5)
