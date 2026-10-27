"""The bounded read protocol does not depend on server isolation defaults."""
import unittest
from unittest.mock import Mock, patch
from tests.helpers import editor_config
from gxp_editor.db import EditorDatabase


class SnapshotReadTests(unittest.TestCase):
    def test_repeatable_consistent_read_only_view_precedes_queries(self):
        database = EditorDatabase(editor_config().database)
        connection = Mock()
        connection.cursor.return_value.execute.return_value = 0
        with patch.object(database, "_connect", return_value=connection):
            with database.read_session() as session:
                session.execute("SELECT synthetic_fixture")
        statements = [call.args[0] for call in connection.cursor.return_value.execute.call_args_list]
        self.assertEqual(statements, ["SET SESSION MAX_EXECUTION_TIME = 5000",
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY", "SELECT synthetic_fixture"])
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_failed_snapshot_initialization_never_yields_partial_evidence(self):
        database = EditorDatabase(editor_config().database)
        connection = Mock()
        connection.cursor.return_value.execute.side_effect = [0, RuntimeError("synthetic failure")]
        entered = False
        with patch.object(database, "_connect", return_value=connection), self.assertRaises(RuntimeError):
            with database.read_session():
                entered = True
        self.assertFalse(entered)
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()
