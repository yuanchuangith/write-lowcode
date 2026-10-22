from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from tests.helpers import baseline
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

    def test_new_queued_transaction_reuses_existing_window_without_spawning(self):
        session = self.add()
        plan = self.store.create_plan(session["id"], "plan.json", "old", "new", "compiler", conversation_id=self.owner)
        tx = self.store.create_transaction(session["id"], plan, "write", str(Path(self.folder.name) / "tx.json"), conversation_id=self.owner)
        service = object.__new__(EditorService)
        service._conversation_id = self.owner
        service.state = self.store
        with patch("gxp_editor.config.runtime_root", return_value=Path(self.folder.name)), patch("gxp_editor.ui_launch.launch_window",
                return_value={"window_opened": True, "window_starting": False}) as launch, patch("gxp_editor.ui_launch.inspect_window_startup",
                return_value={"window_opened": False, "window_starting": False, "startup_status": "unknown"}):
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
