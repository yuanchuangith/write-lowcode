from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from tests.helpers import baseline
from gxp_editor.approval import save_artifact
from gxp_editor.execution_lock import (
    ApprovalWindowBusyError,
    ExecutionBusyError,
    approval_window_lock,
    execution_lock,
)
from gxp_editor.service import EditorService, EditorServiceError
from gxp_editor.state import StateError, StateStore


class SessionIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = StateStore(self.root / "state.sqlite3")
        self.owner_a = self.store.create_conversation("First conversation")["conversation_id"]
        self.owner_b = self.store.create_conversation("Second conversation")["conversation_id"]

    def service(self, owner):
        service = object.__new__(EditorService)
        service.state = self.store
        service._conversation_id = owner
        return service

    def session(self, owner=None, **draft_fields):
        return self.store.create_session(
            baseline(**draft_fields), "backup", "compiler",
            conversation_id=self.owner_a if owner is None else owner,
        )

    def plan(self, session):
        return self.store.create_plan(
            session["id"], str(self.root / "plan.json"), "old", "new", "compiler",
            conversation_id=session["conversation_id"],
            owner_revision=session["owner_revision"],
        )

    def pending(self):
        session = self.session()
        plan = self.plan(session)
        path = self.root / "transaction.json"
        save_artifact(path, {"statements": [], "approvals": {}})
        transaction = self.store.create_transaction(
            session["id"], plan, "write", str(path),
            conversation_id=session["conversation_id"],
            owner_revision=session["owner_revision"],
        )
        return session, plan, transaction, path

    def test_concurrent_different_owners_can_acquire_different_targets(self):
        barrier = Barrier(2)

        def acquire(owner):
            store = StateStore(self.store.path)
            barrier.wait(timeout=5)
            return store.create_session(
                baseline(id="design-" + owner, ref_id="ref-" + owner),
                "backup", "compiler", conversation_id=owner,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            sessions = list(pool.map(acquire, (self.owner_a, self.owner_b)))
        self.assertNotEqual(sessions[0]["id"], sessions[1]["id"])
        for session in sessions:
            self.assertEqual(
                self.store.active_session(session["conversation_id"])["id"], session["id"],
            )

    def test_same_owner_cannot_acquire_a_second_active_target(self):
        original = self.session()
        with self.assertRaises(StateError):
            self.session(id="other-design", ref_id="other-ref")
        self.assertEqual(self.store.active_session(self.owner_a)["id"], original["id"])

    def test_same_design_or_ref_is_exclusive_across_owners(self):
        original = self.session()
        for fields in (
            {"id": original["design_id"], "ref_id": "other-ref"},
            {"id": "other-design", "ref_id": original["ref_id"]},
        ):
            with self.subTest(fields=fields), self.assertRaises(StateError):
                self.session(self.owner_b, **fields)
        self.assertIsNone(self.store.active_session(self.owner_b))

    def test_concurrent_same_target_has_one_winner(self):
        barrier = Barrier(2)

        def acquire(owner):
            store = StateStore(self.store.path)
            barrier.wait(timeout=5)
            try:
                return store.create_session(baseline(), "backup", "compiler", conversation_id=owner)
            except StateError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            sessions = list(pool.map(acquire, (self.owner_a, self.owner_b)))
        self.assertEqual(sum(session is not None for session in sessions), 1)

    def test_status_returns_only_the_current_conversation_without_canvas_content(self):
        first = self.session()
        second = self.session(self.owner_b, id="other-design", ref_id="other-ref")
        for owner, expected in ((self.owner_a, first), (self.owner_b, second)):
            service = self.service(owner)
            with patch.object(service, "_components", side_effect=RuntimeError("configuration absent")):
                result = service.editor_status()
            self.assertEqual(result["active_session"]["id"], expected["id"])
            self.assertEqual(result["conversation_id"], owner)
            self.assertNotIn("baseline_json", result["active_session"])
            self.assertNotIn("baseline", result["active_session"])
        legacy = self.service("")
        with patch.object(legacy, "_components", side_effect=RuntimeError("configuration absent")):
            self.assertIsNone(legacy.editor_status()["active_session"])

    def test_other_conversation_cannot_inspect_change_or_open_a_flow(self):
        session, plan, transaction, _ = self.pending()
        for owner in (self.owner_b, ""):
            service = self.service(owner)
            actions = (
                ("plan_canvas_patch", (session["id"], [])),
                ("inspect_reference_repairs", (session["id"],)),
                ("plan_reference_repairs", (session["id"],)),
                ("prepare_write", (plan,)),
                ("get_write_status", (transaction,)),
                ("reopen_approval", (transaction,)),
                ("recover_edit_session", (session["id"],)),
                ("verify_saved_draft", (session["id"],)),
                ("prepare_restore", (session["id"], "backup")),
                ("finish_edit_session", (session["id"],)),
                ("abort_edit_session", (session["id"],)),
            )
            with patch.object(service, "_components", return_value=(None,) * 5), patch(
                "gxp_editor.service.subprocess.Popen",
            ) as launch:
                for name, arguments in actions:
                    with self.subTest(owner=owner, action=name), self.assertRaises(EditorServiceError):
                        getattr(service, name)(*arguments)
                launch.assert_not_called()
        self.assertEqual(self.store.get_transaction(transaction)["state"], "awaiting_approval")
        self.assertEqual(self.store.get_session(session["id"])["conversation_id"], self.owner_a)

    def test_attach_is_blocked_by_an_open_approval_window_and_succeeds_after_close(self):
        session, _, transaction, path = self.pending()
        service = self.service(self.owner_b)
        with approval_window_lock(path):
            with self.assertRaises(ApprovalWindowBusyError):
                service.attach_edit_session(session["id"])
        self.assertEqual(self.store.get_session(session["id"])["conversation_id"], self.owner_a)
        attached = service.attach_edit_session(session["id"])
        self.assertTrue(attached["attached"])
        self.assertEqual(attached["transaction_id"], transaction)
        self.assertEqual(attached["owner_revision"], 1)
        self.assertFalse(attached["write_executed"])

    def test_attach_is_blocked_by_an_executor_lock(self):
        session, _, _, path = self.pending()
        with execution_lock(path), self.assertRaises(ExecutionBusyError):
            self.service(self.owner_b).attach_edit_session(session["id"])
        self.assertEqual(self.store.get_session(session["id"])["conversation_id"], self.owner_a)
        self.assertTrue(self.service(self.owner_b).attach_edit_session(session["id"])["attached"])

    def test_dead_executor_can_be_transferred_for_recovery(self):
        session, _, transaction, _ = self.pending()
        self.store.claim_transaction(transaction)
        with patch("gxp_editor.process_check.assert_approval_process_stopped"):
            self.assertTrue(self.service(self.owner_b).attach_edit_session(session["id"])["attached"])
        self.assertEqual(self.store.get_session(session["id"])["conversation_id"], self.owner_b)
        self.assertEqual(self.store.get_transaction(transaction)["state"], "executing")

    def test_handoff_preserves_the_approval_and_denies_the_previous_owner(self):
        session, plan, transaction, path = self.pending()
        artifact = {"statements": [], "approvals": {"statement-1": {"approved": True}}}
        save_artifact(path, artifact)
        before = path.read_bytes()
        self.service(self.owner_b).attach_edit_session(session["id"])
        with self.assertRaises(EditorServiceError):
            self.service(self.owner_a).get_write_status(transaction)
        status = self.service(self.owner_b).get_write_status(transaction)
        self.assertEqual(status["approved_count"], 1)
        self.assertEqual(status["session_marker"], session["id"])
        self.assertEqual(self.store.get_plan(plan)["status"], "frozen")
        self.assertEqual(path.read_bytes(), before)

    def test_attach_refuses_to_replace_the_receiving_conversations_active_flow(self):
        first = self.session()
        second = self.session(self.owner_b, id="other-design", ref_id="other-ref")
        with self.assertRaises(StateError):
            self.service(self.owner_b).attach_edit_session(first["id"])
        self.assertEqual(self.store.active_session(self.owner_b)["id"], second["id"])
        self.assertEqual(self.store.get_session(first["id"])["conversation_id"], self.owner_a)

    def test_stale_owner_cannot_create_plan_transaction_abort_or_finish_after_handoff(self):
        session = self.session()
        plan = self.plan(session)
        self.service(self.owner_b).attach_edit_session(session["id"])
        operations = (
            lambda: self.plan(session),
            lambda: self.store.create_transaction(
                session["id"], plan, "write", str(self.root / "stale.json"),
                conversation_id=self.owner_a, owner_revision=0,
            ),
            lambda: self.store.abort_session(session["id"], conversation_id=self.owner_a, owner_revision=0),
            lambda: self.store.update_session(session["id"], "finished", conversation_id=self.owner_a, owner_revision=0),
        )
        for index, operation in enumerate(operations):
            with self.subTest(operation=index), self.assertRaises(StateError):
                operation()
        current = self.store.get_session(session["id"])
        self.assertEqual((current["state"], current["plan_id"]), ("planned", plan))
        self.assertIsNone(current["transaction_id"])
        self.assertEqual(self.store.get_plan(plan)["status"], "ready")

    def test_round_trip_handoff_rejects_a_stale_revision_even_for_the_same_owner(self):
        original = self.session()
        self.service(self.owner_b).attach_edit_session(original["id"])
        self.service(self.owner_a).attach_edit_session(original["id"])
        current = self.store.get_session(original["id"])
        self.assertEqual(current["conversation_id"], self.owner_a)
        self.assertEqual(current["owner_revision"], 2)
        with self.assertRaises(StateError):
            self.plan(original)
        self.assertIsNotNone(self.plan(current))

    def test_transaction_creation_freezes_its_plan_in_the_same_state_transition(self):
        session, plan, transaction, _ = self.pending()
        observer = StateStore(self.store.path)
        current = observer.get_session(session["id"])
        self.assertEqual((current["state"], current["transaction_id"]), ("awaiting_approval", transaction))
        self.assertEqual(observer.get_plan(plan)["status"], "frozen")

    def test_concurrent_handoffs_with_the_same_snapshot_have_one_winner(self):
        session = self.session()
        owner_c = self.store.create_conversation("Third conversation")["conversation_id"]
        barrier = Barrier(2)

        def transfer(owner):
            store = StateStore(self.store.path)
            barrier.wait(timeout=5)
            try:
                return store.transfer_session(
                    session["id"], owner, expected_owner=self.owner_a, expected_revision=0,
                )
            except StateError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(transfer, (self.owner_b, owner_c)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.store.get_session(session["id"])["owner_revision"], 1)

    def test_unknown_conversation_cannot_acquire_or_attach(self):
        with self.assertRaises(StateError):
            self.session("unregistered-conversation")
        session = self.session()
        with self.assertRaises(StateError):
            self.service("unregistered-conversation").attach_edit_session(session["id"])
        self.assertEqual(self.store.get_session(session["id"])["conversation_id"], self.owner_a)

    def test_new_approval_pointer_invalidates_a_handoff_snapshot(self):
        session = self.session()
        plan = self.plan(session)
        transaction = self.store.create_transaction(
            session["id"], plan, "write", str(self.root / "new-transaction.json"),
            conversation_id=self.owner_a, owner_revision=0,
        )
        with self.assertRaises(StateError):
            self.store.transfer_session(
                session["id"], self.owner_b, expected_owner=self.owner_a,
                expected_revision=0, expected_transaction_id=None,
            )
        self.assertEqual(self.store.get_session(session["id"])["transaction_id"], transaction)
        self.assertEqual(self.store.get_session(session["id"])["conversation_id"], self.owner_a)

    def test_omitted_marker_cannot_mutate_a_conversation_owned_flow(self):
        session = self.session()
        plan = self.plan(session)
        operations = (
            lambda: self.store.create_plan(session["id"], "stale.json", "old", "new", "compiler"),
            lambda: self.store.create_transaction(session["id"], plan, "write", "stale.json"),
            lambda: self.store.abort_session(session["id"]),
            lambda: self.store.update_session(session["id"], "finished"),
        )
        for index, operation in enumerate(operations):
            with self.subTest(operation=index), self.assertRaises(StateError):
                operation()
        self.assertEqual(self.store.get_session(session["id"])["state"], "planned")

    def test_concurrent_legacy_schema_migration_preserves_existing_ownership_defaults(self):
        path = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as database:
            database.execute("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, ref_id TEXT NOT NULL, design_id TEXT NOT NULL,
                    action_code TEXT NOT NULL, action_name TEXT NOT NULL, state TEXT NOT NULL,
                    baseline_json TEXT NOT NULL, backup_id TEXT NOT NULL, compiler_hash TEXT NOT NULL,
                    plan_id TEXT, transaction_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
            """)
            database.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("legacy-session", "legacy-ref", "legacy-design", "Legacy", "Legacy action",
                 "locked", json.dumps(baseline()), "legacy-backup", "compiler", None, None,
                 "2026-01-01", "2026-01-01"),
            )
            database.commit()
        barrier = Barrier(4)

        def migrate(_):
            barrier.wait(timeout=5)
            return StateStore(path)

        with ThreadPoolExecutor(max_workers=4) as pool:
            stores = list(pool.map(migrate, range(4)))
        session = stores[0].get_session("legacy-session")
        self.assertEqual(session["conversation_id"], "")
        self.assertEqual(session["owner_revision"], 0)
        self.assertEqual(session["session_mode"], "edit")
        for store in stores:
            self.assertEqual(store.active_session("")["id"], session["id"])

    def test_finish_cannot_close_a_restore_started_after_its_initial_read(self):
        session, _, transaction, _ = self.pending()
        self.store.update_transaction(transaction, "saved")
        self.store.update_session(session["id"], "verified", conversation_id=self.owner_a)
        service = self.service(self.owner_a)
        original = self.store.update_session
        restore = []

        def begin_restore_before_finish(*args, **kwargs):
            restore.append(self.store.create_transaction(
                session["id"], None, "restore", str(self.root / "restore.json"),
                conversation_id=self.owner_a, owner_revision=0,
            ))
            return original(*args, **kwargs)

        with patch.object(self.store, "update_session", side_effect=begin_restore_before_finish), self.assertRaises(StateError):
            service.finish_edit_session(session["id"])
        current = self.store.get_session(session["id"])
        self.assertEqual((current["state"], current["transaction_id"]), ("restore_pending", restore[0]))

    def test_previous_saved_verification_cannot_replace_a_new_restore(self):
        session, _, transaction, _ = self.pending()
        self.store.update_transaction(transaction, "saved")
        self.store.update_session(session["id"], "saved", conversation_id=self.owner_a)
        restore = self.store.create_transaction(
            session["id"], None, "restore", str(self.root / "restore.json"),
            conversation_id=self.owner_a, owner_revision=0,
        )
        with self.assertRaises(StateError):
            self.store.update_session(
                session["id"], "verified", conversation_id=self.owner_a, owner_revision=0,
                expected_states=("saved", "verified"), expected_transaction_id=transaction,
            )
        self.assertEqual(self.store.get_session(session["id"])["transaction_id"], restore)

    def test_cancelled_plan_cannot_create_a_transaction_and_changes_roll_back(self):
        session = self.session()
        plan = self.plan(session)
        self.store.update_plan_status(plan, "cancelled")
        with self.assertRaises(StateError):
            self.store.create_transaction(
                session["id"], plan, "write", "unused.json", transaction_id="rejected",
                conversation_id=self.owner_a, owner_revision=0,
            )
        current = self.store.get_session(session["id"])
        self.assertEqual(current["state"], "planned")
        self.assertIsNone(current["transaction_id"])
        self.assertEqual(self.store.get_plan(plan)["status"], "cancelled")
        with self.assertRaises(StateError):
            self.store.get_transaction("rejected")


if __name__ == "__main__":
    unittest.main()
