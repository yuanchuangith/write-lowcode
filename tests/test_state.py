from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import baseline
from gxp_editor.state import StateError, StateStore


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

    def test_session_rejects_second_active_approval_transaction(self):
        session = self.store.create_session(baseline(), "backup-1", "compiler-1")
        plan = self.store.create_plan(session["id"], "one.json", "old", "new", "compiler-1")
        self.store.create_transaction(session["id"], plan, "write", "tx-one.json", transaction_id="tx-one")
        with self.assertRaisesRegex(StateError, "active approval transaction"):
            self.store.create_transaction(session["id"], plan, "write", "tx-two.json", transaction_id="tx-two")


if __name__ == "__main__":
    unittest.main()
