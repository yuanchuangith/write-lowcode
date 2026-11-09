"""Default form state and backups are partitioned without changing workflows."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

from gxp_form_editor.common import FormError
from gxp_form_editor.state import Store


class FormDatabaseStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch("gxp_form_editor.state.runtime_root", return_value=self.root))
        api = types.ModuleType("gxp_project_context")
        api.current_database_scope = Mock(return_value=None)
        self.scope = api.current_database_scope
        self.enterContext(patch.dict(sys.modules, {"gxp_project_context": api}))

    def test_database_switch_separates_state_and_backups_and_returns_to_original(self):
        self.scope.return_value = "a" * 64
        first = Store()
        owner = first.create_conversation("synthetic")
        snapshot = {"identity": {"page_id": "fixture-page"}, "page": {}}
        backup = first.save_backup(snapshot, b"synthetic-secret")
        self.assertEqual(self.root / "databases" / ("a" * 64), first.root)
        self.scope.assert_called_with(role="write")

        self.scope.return_value = "b" * 64
        other = Store()
        self.assertNotEqual(first.path, other.path)
        with self.assertRaises(FormError):
            other.get("conversation", owner["id"], owner["id"])
        with self.assertRaises(FormError):
            other.load_backup(backup, b"synthetic-secret")

        self.scope.return_value = "a" * 64
        reopened = Store()
        self.assertEqual(owner, reopened.get("conversation", owner["id"], owner["id"]))
        self.assertEqual(snapshot, reopened.load_backup(backup, b"synthetic-secret"))

    def test_explicit_root_keeps_existing_caller_semantics(self):
        self.scope.side_effect = AssertionError("Explicit roots do not resolve project configuration")
        explicit = self.root / "explicit"
        self.assertEqual(explicit, Store(explicit).root)
        self.scope.assert_not_called()

    def test_no_database_uses_legacy_root_without_claiming_it_after_selection(self):
        legacy = Store()
        owner = legacy.create_conversation("legacy")
        self.assertEqual(self.root, legacy.root)
        original = legacy.path.read_bytes()
        self.scope.return_value = "a" * 64
        scoped = Store()
        with self.assertRaises(FormError):
            scoped.get("conversation", owner["id"], owner["id"])
        self.assertEqual(original, legacy.path.read_bytes())

    def test_invalid_scope_cannot_escape_the_storage_root(self):
        self.scope.return_value = "../another-project"
        with self.assertRaises(FormError) as invalid:
            Store()
        self.assertEqual("form_database_scope_invalid", invalid.exception.code)


if __name__ == "__main__":
    unittest.main()
