from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from tests.helpers import editor_config
from gxp_editor.db import EditorDatabase


class DatabaseOutcomeTests(unittest.TestCase):
    def test_rollback_is_confirmed_only_before_commit_is_attempted(self):
        for commit_failure in (False, True):
            with self.subTest(commit_failure=commit_failure):
                connection = Mock()
                connection.cursor.return_value.execute.return_value = 0
                if commit_failure:
                    connection.commit.side_effect = OSError("lost commit acknowledgement")
                database = EditorDatabase(editor_config().database)
                with patch.object(database, "_connect", return_value=connection):
                    with self.assertRaises(OSError):
                        with database.write_transaction() as session:
                            if not commit_failure:
                                raise OSError("statement failed")
                self.assertEqual(session.rollback_confirmed, not commit_failure)
                connection.rollback.assert_called_once()

    def test_rollback_failure_is_not_confirmed(self):
        connection = Mock()
        connection.cursor.return_value.execute.return_value = 0
        connection.rollback.side_effect = OSError("rollback lost")
        database = EditorDatabase(editor_config().database)
        with patch.object(database, "_connect", return_value=connection):
            with self.assertRaisesRegex(OSError, "rollback lost"):
                with database.write_transaction() as session:
                    raise ValueError("statement failed")
        self.assertFalse(session.rollback_confirmed)
