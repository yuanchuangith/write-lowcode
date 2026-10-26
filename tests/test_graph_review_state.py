from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gxp_editor.service import EditorService, EditorServiceError
from gxp_editor.state import StateError, StateStore
from tests.helpers import baseline


class GraphReviewStateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = StateStore(Path(directory.name) / "state.sqlite3")
        self.owner = self.store.create_conversation()["conversation_id"]
        self.other = self.store.create_conversation()["conversation_id"]
        self.queue = self.store.create_edit_queue(self.owner)
        self.session, self.tx = self.add_saved(1)
        self.service = object.__new__(EditorService)
        self.service.state = self.store
        self.service._conversation_id = self.owner

    def add_saved(self, number, *, finish=True):
        session = self.store.create_session(baseline(id=f"design-{number}", ref_id=f"ref-{number}"),
            "backup", "compiler", conversation_id=self.owner, queue_id=self.queue, database_scope="f" * 64)
        plan = self.store.create_plan(session["id"], "plan.json", "old", "new", "compiler", conversation_id=self.owner)
        tx = self.store.create_transaction(session["id"], plan, "write", "missing.json", conversation_id=self.owner)
        self.store.update_transaction(tx, "saved")
        self.store.update_session(session["id"], "finished" if finish else "verified", conversation_id=self.owner)
        return session, tx

    def record(self, outcome, **kwargs):
        return self.service.record_graph_review(self.tx, outcome, **kwargs)["look_graph_review"]

    def test_decline_survives_reopening_and_status_queries_without_granting_consent(self):
        proposed = self.record("proposed", proposal_sha256="a" * 64)
        self.assertEqual(proposed["next_action"], "await_graph_consent")
        declined = self.record("declined", expected_revision=1)
        self.assertFalse(declined["review_required"])
        self.service.state = StateStore(self.store.path)
        with patch.object(self.service, "_components", side_effect=AssertionError("offline metadata only")):
            for _ in range(3):
                status = self.service.get_write_status(self.tx)
                self.assertEqual(status["look_graph_review"]["review_state"], declined["review_state"])
                self.assertEqual(status["next_action"], "report_completion")
                self.assertEqual(status["look_graph_review"]["database_scope"], "f" * 64)
                self.assertFalse(status["look_graph_review"]["graph_write_executed"])
        with self.assertRaisesRegex(StateError, "declined"):
            self.record("proposed", expected_revision=2, proposal_sha256="a" * 64)

    def test_decline_cannot_be_erased_by_intermediate_outcome_but_user_can_reconsider(self):
        self.record("proposed", proposal_sha256="a" * 64)
        self.record("declined", expected_revision=1)
        self.record("no_difference", expected_revision=2)
        with self.assertRaisesRegex(StateError, "declined"):
            self.record("proposed", expected_revision=3, proposal_sha256="a" * 64)
        result = self.service.record_graph_review(self.tx, "proposed", expected_revision=3,
            proposal_sha256="a" * 64, reconsider_declined=True)
        self.assertFalse(result["consent_granted"])
        self.assertFalse(result["graph_write_executed"])

    def test_new_proposal_does_not_inherit_refusal_or_approval(self):
        self.record("proposed", proposal_sha256="a" * 64)
        self.record("declined", expected_revision=1)
        changed = self.record("proposed", expected_revision=2, proposal_sha256="b" * 64)
        self.assertTrue(changed["requires_user_confirmation"])
        self.assertEqual(changed["review_state"]["proposal_sha256"], "b" * 64)

    def test_cas_and_owner_guards_preserve_current_state(self):
        self.record("no_difference")
        with self.assertRaisesRegex(StateError, "revision"):
            self.record("pending_publication")
        self.service._conversation_id = self.other
        with self.assertRaises(EditorServiceError):
            self.record("no_difference", expected_revision=1)
        with self.assertRaises(StateError):
            self.store.record_graph_review(self.tx, "no_difference", conversation_id=self.other, expected_revision=1)
        self.assertEqual(self.store.graph_review(self.tx)["revision"], 1)

    def test_unfinished_and_previous_restore_transactions_cannot_be_reviewed(self):
        self.store.update_session(self.session["id"], "verified", conversation_id=self.owner)
        with self.assertRaisesRegex(StateError, "released"):
            self.record("no_difference")
        restore = self.store.create_transaction(self.session["id"], None, "restore", "restore.json", conversation_id=self.owner)
        self.store.update_transaction(restore, "saved")
        self.store.update_session(self.session["id"], "finished", conversation_id=self.owner)
        with self.assertRaisesRegex(StateError, "current"):
            self.record("no_difference")
        self.assertIsNone(self.service.get_write_status(self.tx)["look_graph_review"])

    def test_recording_success_requires_graph_receipt_revision_and_no_proposal_body(self):
        for kwargs in ({"proposal_sha256": "raw relation text"}, {"proposal_sha256": "a" * 64},
                       {"proposal_sha256": "a" * 64, "graph_revision": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(StateError):
                self.record("recorded", **kwargs)
        result = self.record("recorded", proposal_sha256="a" * 64, graph_revision=7)
        self.assertFalse(result["review_required"])
        self.assertEqual(result["review_state"]["graph_revision"], 7)
        self.assertTrue(result["requires_user_confirmation"])

    def test_pending_publication_resumes_without_treating_it_as_graph_consent(self):
        pending = self.record("pending_publication", proposal_sha256="a" * 64)
        self.assertEqual(pending["next_action"], "wait_for_publication")
        self.assertEqual(self.store.graph_review(self.tx)["outcome"], "pending_publication")
        proposed = self.record("proposed", expected_revision=1)
        self.assertEqual(proposed["next_action"], "await_graph_consent")
        self.assertTrue(proposed["requires_user_confirmation"])

    def test_queue_combines_completed_reviews_and_keeps_prior_success_after_later_failure(self):
        second, second_tx = self.add_saved(2)
        third, third_tx = self.add_saved(3)
        self.store.update_transaction(third_tx, "failed_no_write")
        self.store.update_session(third["id"], "failed_no_write", conversation_id=self.owner)
        self.record("no_difference")
        result = self.service.get_edit_queue(self.queue)
        self.assertEqual([r["transaction_id"] for r in result["look_graph_reviews"]], [self.tx, second_tx])
        self.assertEqual(result["pending_graph_review_count"], 1)
        self.assertEqual(len(result["publication_targets"]), 2)
        transferred, transferred_tx = self.add_saved(4, finish=False)
        self.store.transfer_session(transferred["id"], self.other, expected_owner=self.owner,
                                    expected_revision=0, expected_transaction_id=transferred_tx)
        self.store.update_session(transferred["id"], "finished", conversation_id=self.other)
        result = self.service.get_edit_queue(self.queue)
        self.assertEqual([r["transaction_id"] for r in result["look_graph_reviews"]], [self.tx, second_tx])

    def test_mcp_forwards_review_revision_and_has_no_approval_side_effect(self):
        from tests.test_conversation_tools import SERVER
        with patch.object(SERVER, "service", return_value=self.service):
            _, result = asyncio.run(SERVER.mcp.call_tool("record_graph_review", {
                "transaction_id": self.tx, "outcome": "no_difference", "conversation_id": self.owner}))
        self.assertFalse(result["consent_granted"])
        self.assertFalse(result["write_executed"])
        self.assertFalse(result["look_graph_review"]["review_required"])
