from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import baseline, editor_config, transaction
from gxp_editor.approval import approve_statement, load_artifact, save_artifact
from gxp_editor.approval import transaction_binding_hash
from gxp_editor.hashing import design_content_hash
from gxp_editor.writer import WriteExecutionError, execute_approved_transaction


class FakeState:
    def __init__(self, artifact_path: str):
        self.transaction = {
            "id": "transaction-1",
            "session_id": "session-1",
            "state": "awaiting_approval",
            "artifact_path": artifact_path,
        }
        self.session_state = "awaiting_approval"

    def get_transaction(self, transaction_id):
        if transaction_id != self.transaction["id"]:
            raise AssertionError("unexpected transaction")
        return dict(self.transaction)

    def update_transaction(self, transaction_id, state, message=""):
        self.transaction["state"] = state
        self.transaction["message"] = message

    def update_session(self, session_id, state, **fields):
        self.session_state = state

    def claim_transaction(self, transaction_id):
        if self.transaction["state"] != "awaiting_approval":
            raise WriteExecutionError("Transaction cannot be executed")
        self.update_transaction(transaction_id, "executing")

    def release_failed_transaction(self, transaction_id, outcome, message):
        self.update_transaction(transaction_id, outcome, message)
        self.session_state = outcome


class FakeSession:
    def __init__(self, affected_rows: int):
        self.affected_rows = affected_rows
        self.executed = []

    def execute(self, sql, params):
        self.executed.append((sql, params))
        return self.affected_rows


class FakeDatabase:
    def __init__(self, session: FakeSession):
        self.session = session

    @contextlib.contextmanager
    def write_transaction(self):
        yield self.session


class FakeRepository:
    def __init__(self, current, saved, relations=None, grants_sha256="c" * 64):
        self.current = current
        self.saved = saved
        self.relations = list(relations or [])
        self.grants_sha256 = grants_sha256
        self.loads = 0

    def grant_status(self, *, account_mode="dedicated", expected_grants_sha256=""):
        return {
            "safe": True,
            "account_mode": account_mode,
            "grants_sha256": self.grants_sha256,
        }

    def load_draft_by_id(self, design_id, *, for_update=False, session=None):
        self.loads += 1
        return dict(self.current if self.loads == 1 else self.saved)

    def _relations(self, design_id, session):
        return list(self.relations)

    def check_statement_targets(self, statements, session):
        from gxp_editor.single_row_checker import check_statement_list
        check_statement_list(statements)


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = editor_config()
        self.artifact = transaction()
        with patch("gxp_editor.approval.approval_secret", return_value="test-secret"):
            for statement in self.artifact["statements"]:
                approve_statement(self.config, self.artifact, statement["id"])
        self.path = Path(self.temporary.name) / "transaction.json"
        save_artifact(self.path, self.artifact)
        self.state = FakeState(str(self.path))

    def saved_row(self):
        row = baseline()
        row.update(self.artifact["desired_design"])
        row["last_modification_time"] = self.artifact["desired_last_modification_time"]
        row["content_hash"] = design_content_hash(row)
        self.assertEqual(row["content_hash"], self.artifact["new_content_hash"])
        return row

    @contextlib.contextmanager
    def patches(self, repository, session):
        with patch("gxp_editor.writer.load_config", return_value=self.config), \
            patch("gxp_editor.writer.StateStore", return_value=self.state), \
            patch("gxp_editor.writer.EditorDatabase", return_value=FakeDatabase(session)), \
            patch("gxp_editor.writer.EditorRepository", return_value=repository), \
            patch("gxp_editor.approval.approval_secret", return_value="test-secret"):
            yield

    def test_concurrent_change_consumes_transaction_without_writing(self):
        changed = baseline(js_code="changed-by-someone-else")
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(changed, self.saved_row())
        with self.patches(repository, session):
            with self.assertRaisesRegex(WriteExecutionError, "changed after planning"):
                execute_approved_transaction("transaction-1")
            with self.assertRaisesRegex(WriteExecutionError, "cannot be executed"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(session.executed, [])
        self.assertTrue(load_artifact(self.path)["execution_started"])
        self.assertEqual(self.state.session_state, "failed_no_write")

    def test_cancelled_transaction_cannot_write(self):
        self.state.transaction["state"] = "cancelled"
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())
        with self.patches(repository, session):
            with self.assertRaisesRegex(WriteExecutionError, "cannot be executed"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(session.executed, [])

    def test_cancellation_during_approval_verification_blocks_claim(self):
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())
        with self.patches(repository, session), patch(
            "gxp_editor.writer.verify_approvals",
            side_effect=lambda *args: self.state.update_transaction("transaction-1", "cancelled"),
        ):
            with self.assertRaisesRegex(WriteExecutionError, "cannot be executed"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(session.executed, [])
        self.assertFalse(load_artifact(self.path).get("execution_started", False))

    def test_target_preflight_failure_blocks_all_writes(self):
        from gxp_editor.repository import RepositoryError
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())
        with self.patches(repository, session), patch.object(repository, "check_statement_targets", side_effect=RepositoryError("已阻止修改：实际匹配 2 条数据")):
            with self.assertRaisesRegex(RepositoryError, "实际匹配 2"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(session.executed, [])
        self.assertEqual(repository.loads, 0)
        self.assertEqual(self.state.transaction["state"], "failed_no_write")

    def test_multiple_affected_rows_abort_before_next_statement(self):
        session = FakeSession(affected_rows=2)
        repository = FakeRepository(baseline(), self.saved_row())
        with self.patches(repository, session):
            with self.assertRaisesRegex(WriteExecutionError, "affected 2 rows"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(self.state.transaction["state"], "failed")

    def test_unexpected_affected_rows_rolls_back_and_blocks_reuse(self):
        session = FakeSession(affected_rows=0)
        repository = FakeRepository(baseline(), self.saved_row())
        with self.patches(repository, session):
            with self.assertRaisesRegex(WriteExecutionError, "affected 0 rows"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(self.state.transaction["state"], "failed")
        self.assertTrue(load_artifact(self.path)["execution_started"])

    def test_development_build_evidence_does_not_block_database_write(self):
        self.artifact["runtime_validation"].update(
            {
                "available": False,
                "success": False,
                "production_compatible": False,
                "source_mode": "development_build",
                "reference_manifest_sha256": "",
                "warning": "local compilation unavailable",
            }
        )
        self.artifact["approvals"] = {}
        self.artifact["transaction_hash"] = transaction_binding_hash(self.artifact)
        with patch("gxp_editor.approval.approval_secret", return_value="test-secret"):
            for statement in self.artifact["statements"]:
                approve_statement(self.config, self.artifact, statement["id"])
        save_artifact(self.path, self.artifact)
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(
            baseline(),
            self.saved_row(),
            self.artifact["expected_relation_rows"],
        )
        with self.patches(repository, session):
            result = execute_approved_transaction("transaction-1")
        self.assertEqual(result["state"], "saved")
        self.assertTrue(result["manual_publication_required"])
        self.assertEqual(self.state.transaction["state"], "saved")

    def test_database_grant_drift_blocks_database_write(self):
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(
            baseline(),
            self.saved_row(),
            grants_sha256="d" * 64,
        )
        with self.patches(repository, session):
            with self.assertRaisesRegex(WriteExecutionError, "grants changed"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(session.executed, [])
        self.assertEqual(self.state.transaction["state"], "failed_no_write")
        self.assertTrue(load_artifact(self.path)["execution_started"])

    def test_confirmed_rollback_releases_session_but_uncertain_failure_does_not(self):
        for confirmed in (True, False):
            with self.subTest(rollback_confirmed=confirmed):
                save_artifact(self.path, self.artifact)
                self.state = FakeState(str(self.path))
                session = FakeSession(affected_rows=0)
                session.rollback_confirmed = confirmed
                repository = FakeRepository(baseline(), self.saved_row())
                with self.patches(repository, session):
                    with self.assertRaisesRegex(WriteExecutionError, "affected 0 rows"):
                        execute_approved_transaction("transaction-1")
                self.assertEqual(self.state.transaction["state"], "rolled_back" if confirmed else "failed")
                self.assertEqual(self.state.session_state, "rolled_back" if confirmed else "awaiting_approval")

    def test_successful_write_does_not_require_a_backend_finalizer(self):
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row(), self.artifact["expected_relation_rows"])
        with self.patches(repository, session):
            result = execute_approved_transaction("transaction-1")
        self.assertEqual(result["state"], "saved")
        self.assertFalse(result["runtime_finalized"])
        self.assertEqual(self.state.transaction["state"], "saved")
        self.assertEqual(self.state.session_state, "saved")
        stored = load_artifact(self.path)
        self.assertTrue(stored["executed"])
        self.assertTrue(stored["execution_started"])


if __name__ == "__main__":
    unittest.main()
