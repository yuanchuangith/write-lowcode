from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from tests.helpers import baseline, transaction
from gxp_editor.approval import save_artifact, transaction_binding_hash
from gxp_editor.execution_lock import ApprovalWindowBusyError, approval_queue_lock
from gxp_editor.service import EditorService
from gxp_editor.state import StateError, StateStore


class EditQueueTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = StateStore(Path(self.folder.name) / "state.sqlite3")
        self.owner = self.store.create_conversation()["conversation_id"]
        self.other = self.store.create_conversation()["conversation_id"]
        self.queue = self.store.create_edit_queue(self.owner)
        self.enterContext(patch("gxp_editor.connection_settings.active_settings", return_value=None))

    def add(self, number=1, **kwargs):
        return self.store.create_session(baseline(id=f"design-{number}", ref_id=f"ref-{number}"), "backup", "compiler",
                                         conversation_id=self.owner, queue_id=self.queue, **kwargs)

    def test_explicit_queue_allows_distinct_drafts_and_preserves_target_exclusivity(self):
        first = self.add()
        second = self.add(2)
        self.assertEqual([item["id"] for item in self.store.edit_queue(self.queue, self.owner)], [first["id"], second["id"]])
        with self.assertRaisesRegex(StateError, "Another edit session"):
            self.add()
        with self.assertRaisesRegex(StateError, "Another edit session"):
            self.store.create_session(baseline(id="design-3", ref_id="ref-3"), "backup", "compiler", conversation_id=self.owner)
        with self.assertRaisesRegex(StateError, "Another edit session"):
            self.store.create_session(baseline(), "backup", "compiler", conversation_id=self.other)

    def test_queue_cannot_read_or_add_another_dialogues_session(self):
        with self.assertRaises(StateError):
            self.store.create_edit_queue("")
        with self.assertRaises(StateError):
            self.store.edit_queue(self.queue, self.other)
        with self.assertRaises(StateError):
            self.store.create_session(baseline(), "backup", "compiler", conversation_id=self.other, queue_id=self.queue)
        self.assertEqual(self.store.edit_queue(self.queue, self.owner), [])

    def test_one_dialogue_cannot_mix_active_queues(self):
        self.add()
        another_queue = self.store.create_edit_queue(self.owner)
        with self.assertRaises(StateError):
            self.store.create_session(baseline(id="design-2", ref_id="ref-2"), "backup", "compiler",
                                      conversation_id=self.owner, queue_id=another_queue)

    def test_concurrent_target_claim_in_queue_has_one_winner(self):
        barrier = Barrier(2)
        def claim(_):
            barrier.wait(timeout=5)
            try:
                return self.add()["id"]
            except StateError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, range(2)))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(len(self.store.edit_queue(self.queue, self.owner)), 1)

    def test_transferred_item_keeps_original_queue_revision(self):
        session = self.add()
        self.store.transfer_session(session["id"], self.other, expected_owner=self.owner, expected_revision=0)
        item = self.store.edit_queue(self.queue, self.owner)[0]
        self.assertEqual(item["queued_owner_revision"], 0)
        self.assertEqual(item["owner_revision"], 1)
        self.assertEqual(item["conversation_id"], self.other)

    def test_takeover_joins_current_queue_without_releasing_other_draft(self):
        existing = self.add()
        old_queue = self.store.create_edit_queue(self.other)
        source = self.store.create_session(baseline(id="public", ref_id="public-ref"), "old-backup", "compiler",
            conversation_id=self.other, queue_id=old_queue)
        before = self.store.get_session(source["id"])
        moved = self.store.transfer_session(source["id"], self.owner, expected_owner=self.other,
            expected_revision=0, queue_id=self.queue)
        items = self.store.edit_queue(self.queue, self.owner)
        self.assertEqual([item["id"] for item in items], [existing["id"], source["id"]])
        self.assertEqual(self.store.get_session(existing["id"])["state"], "locked")
        self.assertEqual((moved["owner_revision"], moved["queue_id"]), (1, self.queue))
        self.assertEqual(self.store.get_session(source["id"])["baseline"], before["baseline"])
        self.assertEqual(self.store.edit_queue(old_queue, self.other), [])
        repeated = self.store.transfer_session(source["id"], self.owner, expected_owner=self.owner,
            expected_revision=1, queue_id=self.queue)
        self.assertEqual(repeated["owner_revision"], 1)
        self.assertEqual(self.store.edit_queue(self.queue, self.owner), items)
        with self.assertRaises(StateError):
            self.store.create_plan(source["id"], "old.json", "old", "new", "compiler",
                conversation_id=self.other, owner_revision=0)

    def test_takeover_refuses_foreign_queue_or_active_flow_outside_target_queue(self):
        existing = self.add()
        source = self.store.create_session(baseline(id="public", ref_id="public-ref"), "backup", "compiler", conversation_id=self.other)
        foreign = self.store.create_edit_queue(self.other)
        another = self.store.create_edit_queue(self.owner)
        for queue_id in ("", foreign, another):
            with self.subTest(queue_id=queue_id), self.assertRaises(StateError):
                self.store.transfer_session(source["id"], self.owner, expected_owner=self.other,
                    expected_revision=0, queue_id=queue_id)
        self.assertEqual(self.store.get_session(source["id"])["conversation_id"], self.other)
        self.assertEqual(self.store.get_session(existing["id"])["state"], "locked")

    def test_queue_takeover_preserves_pending_plan_artifact_and_approval(self):
        self.add()
        source = self.store.create_session(baseline(id="public", ref_id="public-ref"), "backup", "compiler", conversation_id=self.other)
        plan = self.store.create_plan(source["id"], "plan.json", "old", "new", "compiler", conversation_id=self.other)
        artifact = Path(self.folder.name) / "approved.json"
        artifact.write_text('{"approvals":{"statement":"signed"}}', encoding="utf-8")
        tx = self.store.create_transaction(source["id"], plan, "write", str(artifact), conversation_id=self.other)
        before = artifact.read_bytes()
        service = object.__new__(EditorService)
        service._conversation_id, service.state = self.owner, self.store
        with patch.object(service, "_components", side_effect=RuntimeError("No business IO in fixture")), \
                patch("gxp_editor.process_check.assert_approval_process_stopped"):
            result = service.attach_edit_session(source["id"], queue_id=self.queue)
        self.assertTrue(result["attached"])
        self.assertEqual(result["queue_id"], self.queue)
        self.assertEqual(artifact.read_bytes(), before)
        self.assertEqual(self.store.get_transaction(tx)["state"], "awaiting_approval")
        self.assertEqual(self.store.get_plan(plan)["status"], "frozen")

    def test_queue_takeover_refuses_running_source_window(self):
        self.add()
        old_queue = self.store.create_edit_queue(self.other)
        source = self.store.create_session(baseline(id="public", ref_id="public-ref"), "backup", "compiler",
            conversation_id=self.other, queue_id=old_queue)
        service = object.__new__(EditorService)
        service._conversation_id, service.state = self.owner, self.store
        with patch("gxp_editor.config.runtime_root", return_value=Path(self.folder.name)), approval_queue_lock(old_queue):
            with self.assertRaises(ApprovalWindowBusyError):
                service.attach_edit_session(source["id"], queue_id=self.queue)
        self.assertEqual(self.store.get_session(source["id"])["conversation_id"], self.other)

    def test_queue_takeover_refuses_active_destination_window(self):
        self.add()
        source = self.store.create_session(baseline(id="public", ref_id="public-ref"), "backup", "compiler",
            conversation_id=self.other)
        service = object.__new__(EditorService)
        service._conversation_id, service.state = self.owner, self.store
        with patch("gxp_editor.config.runtime_root", return_value=Path(self.folder.name)), approval_queue_lock(self.queue):
            with self.assertRaises(ApprovalWindowBusyError):
                service.attach_edit_session(source["id"], queue_id=self.queue)
        self.assertEqual(self.store.get_session(source["id"])["conversation_id"], self.other)

    def test_stale_empty_takeover_guides_normal_finish_instead_of_cancellation(self):
        source = self.store.create_session(baseline(id="public", ref_id="public-ref"), "backup", "compiler", conversation_id=self.other)
        service = object.__new__(EditorService)
        service._conversation_id, service.state = self.owner, self.store
        with patch.object(service, "_components", return_value=(None, None, None, None, None)), \
                patch.object(service, "_baseline_snapshot", return_value=(None, None, {"status": "stale", "checked_at": "fixture"})):
            result = service.attach_edit_session(source["id"], queue_id=self.queue)
        self.assertEqual(result["baseline_comparison"]["minimum_next_step"]["tool"], "finish_edit_session")
        self.assertEqual(self.store.get_session(source["id"])["state"], "locked")

    def test_old_queue_generation_cannot_claim_transferred_transaction(self):
        from gxp_editor.queue_protocol import TOKEN_ENV, GENERATION_ENV
        old_queue = self.store.create_edit_queue(self.other)
        source = self.store.create_session(baseline(id="public", ref_id="public-ref"), "backup", "compiler",
            conversation_id=self.other, queue_id=old_queue)
        plan = self.store.create_plan(source["id"], "plan.json", "old", "new", "compiler", conversation_id=self.other)
        tx = self.store.create_transaction(source["id"], plan, "write", "tx.json", conversation_id=self.other)
        with self.store._connection() as db:
            db.execute("UPDATE queue_runtime SET generation=1,launch_token='old-token',launch_state='active' WHERE queue_id=?", (old_queue,))
            db.execute("UPDATE queue_runtime SET generation=1,launch_token='new-token',launch_state='active' WHERE queue_id=?", (self.queue,))
        self.store.transfer_session(source["id"], self.owner, expected_owner=self.other, expected_revision=0,
            expected_transaction_id=tx, queue_id=self.queue)
        with patch.dict("os.environ", {TOKEN_ENV: "old-token", GENERATION_ENV: "1"}):
            with self.assertRaisesRegex(StateError, "generation changed"):
                self.store.claim_transaction(tx)
        self.assertEqual(self.store.get_transaction(tx)["state"], "awaiting_approval")

    def test_new_queued_transaction_reuses_existing_window_without_spawning(self):
        session = self.add()
        plan = self.store.create_plan(session["id"], "plan.json", "old", "new", "compiler", conversation_id=self.owner)
        tx = self.store.create_transaction(session["id"], plan, "write", str(Path(self.folder.name) / "tx.json"), conversation_id=self.owner)
        artifact = transaction()
        artifact.update(transaction_id=tx, session_id=session["id"], plan_id=plan)
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        save_artifact(Path(self.folder.name) / "tx.json", artifact)
        service = object.__new__(EditorService)
        service._conversation_id = self.owner
        service.state = self.store
        with patch("gxp_editor.config.runtime_root", return_value=Path(self.folder.name)), patch("gxp_editor.ui_launch.launch_window",
                return_value={"window_opened": True, "window_starting": False}) as launch, patch("gxp_editor.ui_launch.inspect_window_startup",
                return_value={"window_opened": False, "window_starting": False, "startup_status": "not_started"}), \
                patch.object(service, "_approval_preflight", return_value={}):
            with approval_queue_lock(self.queue):
                result = service._try_launch_approval(tx)
                self.assertTrue(result["already_open"])
                self.assertFalse(result["approval_window_opened"])
                launch.assert_not_called()
            result = service._try_launch_approval(tx)
            self.assertTrue(result["approval_window_opened"])
            self.assertEqual(launch.call_args.kwargs["arguments"], ["--queue", self.queue, self.owner])

    def test_queue_lock_and_saved_state_survive_store_reopening(self):
        self.add()
        reopened = StateStore(self.store.path)
        self.assertEqual(reopened.edit_queue(self.queue, self.owner), self.store.edit_queue(self.queue, self.owner))
        with patch("gxp_editor.config.runtime_root", return_value=Path(self.folder.name)):
            with approval_queue_lock(self.queue):
                with self.assertRaises(ApprovalWindowBusyError):
                    with approval_queue_lock(self.queue):
                        self.fail("Queue opened twice")


if __name__ == "__main__":
    unittest.main()
