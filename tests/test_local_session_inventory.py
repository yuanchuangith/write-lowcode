"""Local session listing uses only temporary SQLite fixtures, without migration."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from gxp_project_context.local_sessions import LocalSessionError, active_sessions


class LocalSessionInventoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.action = self.root / "actions.sqlite3"
        self.forms = self.root / "forms"
        self.enterContext(patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.root / "local"),
            "GXP_LOWCODE_EDITOR_STATE": str(self.action),
            "GXP_FORM_CONFIG_ROOT": str(self.forms),
        }))

    def action_fixture(self):
        with closing(sqlite3.connect(self.action)) as db:
            db.execute("CREATE TABLE sessions (id TEXT,state TEXT,conversation_id TEXT,action_name TEXT,action_code TEXT,updated_at TEXT)")
            db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?)", [
                ("active", "awaiting_approval", "owner", "Synthetic action", "CODE", "fixture-time"),
                ("finished", "finished", "owner", "Finished", "DONE", "fixture-time"),
            ])
            db.execute("CREATE TABLE transactions (id TEXT,state TEXT,session_id TEXT,updated_at TEXT)")
            db.executemany("INSERT INTO transactions VALUES (?,?,?,?)", [
                ("linked", "executing", "active", "fixture-time"),
                ("orphan", "executing", "missing", "fixture-time"),
                ("saved", "saved", "finished", "fixture-time"),
            ])
            db.commit()

    def form_fixture(self, path, documents):
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE records (kind TEXT,id TEXT,owner TEXT,document TEXT)")
            for kind, marker, document in documents:
                db.execute("INSERT INTO records VALUES (?,?,?,?)", (kind, marker, "form-owner", json.dumps(document)))
            db.commit()

    def test_missing_files_are_empty_without_creating_state(self):
        self.assertEqual([], active_sessions())
        self.assertFalse(self.action.exists())
        self.assertFalse(self.forms.exists())

    def test_action_rows_are_readonly_deduplicated_and_include_orphan_executor(self):
        self.action_fixture()
        before = self.action.read_bytes()
        rows = active_sessions()
        self.assertEqual({"active", "orphan"}, {row["id"] for row in rows})
        action = next(row for row in rows if row["id"] == "active")
        self.assertEqual(("action", "session", "owner", "Synthetic action"),
                         (action["domain"], action["kind"], action["conversation_id"], action["action_name"]))
        self.assertEqual("executing", action["state"])
        self.assertEqual("transaction", next(row for row in rows if row["id"] == "orphan")["kind"])
        self.assertEqual(before, self.action.read_bytes())

    def test_legacy_and_partitioned_forms_are_listed_without_snapshot_values(self):
        active = {"state": "planned", "session_released": False, "target": {"route": "fixture/form"},
                  "snapshot": {"private": "NEVER_RETURN_SNAPSHOT"}}
        released = {"state": "saved", "session_released": True}
        legacy = self.forms / "state.sqlite3"
        scoped = self.forms / "databases" / ("a" * 64) / "state.sqlite3"
        self.form_fixture(legacy, [("session", "legacy", active), ("session", "released", released)])
        self.form_fixture(scoped, [("session", "scoped", active),
                                  ("write", "orphan-write", {"state": "uncertain", "session_id": "missing"})])
        before = {path: path.read_bytes() for path in (legacy, scoped)}
        rows = active_sessions()
        self.assertEqual({"legacy", "scoped", "orphan-write"}, {row["id"] for row in rows})
        self.assertNotIn("NEVER_RETURN_SNAPSHOT", json.dumps(rows))
        self.assertEqual("a" * 64, next(row for row in rows if row["id"] == "scoped")["database_scope"])
        for path, content in before.items():
            self.assertEqual(content, path.read_bytes())

    def test_corrupt_state_is_not_reported_as_no_active_sessions(self):
        self.action.write_bytes(b"not a sqlite database")
        with self.assertRaises(LocalSessionError):
            active_sessions()
        self.assertEqual(b"not a sqlite database", self.action.read_bytes())

    def test_exceeded_limit_does_not_return_a_complete_looking_partial_list(self):
        self.action_fixture()
        with self.assertRaises(LocalSessionError):
            active_sessions(limit=1)


if __name__ == "__main__":
    unittest.main()
