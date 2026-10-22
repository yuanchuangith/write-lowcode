"""Retries resume an exact owned draft without opening a cancellation window."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gxp_editor.service import EditorService
from gxp_editor.state import StateStore, StateError
from tests.helpers import baseline, editor_config
from tests.test_service import FakeCompiler


class SessionRetryTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.store = StateStore(Path(self.folder) / "state.sqlite3")
        self.owner = self.store.create_conversation("retry")["conversation_id"]
        self.draft = baseline(data_json={"actionData": [], "globalVariables": []}, csharp_code="")
        self.compiler = FakeCompiler()
        self.binding = self.compiler.compiler_binding()
        self.service = object.__new__(EditorService)
        self.service.state = self.store
        self.service._conversation_id = self.owner
        self.repository = Mock()
        self.repository.load_draft_by_identifier.return_value = copy.deepcopy(self.draft)
        self.repository.load_relations.return_value = []
        self.enterContext(patch.object(self.service, "_components", return_value=(
            editor_config(), object(), self.repository, self.compiler, object())))
        self.abort = self.enterContext(patch.object(self.service, "abort_edit_session"))

    def create(self, **kwargs):
        return self.store.create_session(self.draft, "backup-1", self.binding["compiler_binding_sha256"],
                                         conversation_id=self.owner, **kwargs)

    def test_same_action_retry_retains_session_backup_and_pending_plan(self):
        session = self.create()
        plan_id = self.store.create_plan(session["id"], "plan.json", "old", "new", session["compiler_hash"],
                                          conversation_id=self.owner)
        with patch.object(self.store, "create_session", side_effect=AssertionError("new session")):
            result = self.service.begin_edit_session(self.draft["code"])
        self.assertTrue(result["session_reused"])
        self.assertEqual(result["session_id"], session["id"])
        self.assertEqual(result["backup_id"], "backup-1")
        self.assertEqual(result["plan_id"], plan_id)
        self.assertEqual(result["next_tool"], "plan_canvas_patch")
        self.assertFalse(result["approval_granted"])
        self.abort.assert_not_called()

    def test_same_queue_retry_and_pending_approval_are_reused(self):
        queue = self.store.create_edit_queue(self.owner)
        session = self.create(queue_id=queue)
        plan = self.store.create_plan(session["id"], "plan.json", "old", "new", session["compiler_hash"],
                                       conversation_id=self.owner)
        tx = self.store.create_transaction(session["id"], plan, "write", "tx.json", conversation_id=self.owner)
        result = self.service.begin_edit_session(self.draft["ref_id"], queue_id=queue)
        self.assertEqual(result["transaction_id"], tx)
        self.assertEqual(result["next_tool"], "get_write_status")
        self.assertEqual(len(self.store.session_transactions(session["id"])), 1)
        self.abort.assert_not_called()

    def test_relation_or_content_or_compiler_drift_does_not_create_or_cancel(self):
        session = self.create()
        for kind in ("relation", "content", "compiler"):
            with self.subTest(kind=kind):
                self.repository.load_relations.return_value = [{"id": "changed"}] if kind == "relation" else []
                self.repository.load_draft_by_identifier.return_value = {**self.draft,
                    **({"content_hash": "changed"} if kind == "content" else {})}
                with patch.object(self.compiler, "compiler_binding", return_value=(
                        {"compiler_binding_sha256": "changed"} if kind == "compiler" else self.binding)):
                    result = self.service.begin_edit_session(self.draft["code"])
                self.assertTrue(result["blocked"])
                self.assertFalse(result["session_created"])
        self.assertEqual(self.store.get_session(session["id"])["state"], "locked")
        self.abort.assert_not_called()

    def test_mode_mismatch_is_explicit_and_does_not_cancel(self):
        self.create()
        result = self.service.begin_reference_repair_session(self.draft["code"])
        self.assertEqual(result["reason_code"], "session_retry_mismatch")
        self.abort.assert_not_called()

    def test_review_checkpoint_requires_correct_owner_revision_and_compare_swap(self):
        session = self.create()
        sha = self.store.save_review_spec_checkpoint(session["id"], {"revision": "r1"}, expected_sha256=None,
                                                     conversation_id=self.owner, owner_revision=0)
        for kwargs in ({"conversation_id": "wrong"}, {"conversation_id": self.owner, "owner_revision": 1}):
            with self.assertRaises(StateError):
                self.store.review_spec_checkpoint(session["id"], **kwargs)
        with self.assertRaisesRegex(StateError, "另一请求更新"):
            self.store.save_review_spec_checkpoint(session["id"], {}, expected_sha256=None,
                                                   conversation_id=self.owner)
        with self.assertRaisesRegex(StateError, "已确认要求已变化"):
            self.store.create_plan(session["id"], "unused", "old", "new", session["compiler_hash"],
                                   conversation_id=self.owner, expected_review_checkpoint="stale")
        self.assertEqual(self.store.review_spec_checkpoint(session["id"], conversation_id=self.owner)["sha256"], sha)


if __name__ == "__main__":
    unittest.main()
