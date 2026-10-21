from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch

from tests.helpers import baseline, editor_config, relation, transaction
from gxp_editor.approval import approve_statement, load_artifact, save_artifact
from gxp_editor.approval import approval_signature, statement_hash, transaction_binding_hash
from gxp_editor.hashing import design_content_hash
from gxp_editor.writer import WriteExecutionError, execute_approved_transaction
from gxp_editor.service import EditorService
from gxp_editor.state import StateStore


class FakeState:
    def __init__(self, artifact_path: str):
        self.transaction = {
            "id": "transaction-1",
            "session_id": "session-1",
            "state": "awaiting_approval",
            "artifact_path": artifact_path,
        }
        self.session_state = "awaiting_approval"
        self.verification = None

    def get_session(self, session_id, **kwargs):
        return {"conversation_id": "owner", "owner_revision": 0}

    def finish_verified_save(self, transaction_id, **evidence):
        self.verification = evidence
        self.session_state = "finished"
        return evidence

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
    def __init__(self, current, saved, relations=None, grants_sha256="c" * 64, current_relations=None,
                 committed=None, committed_relations=None):
        self.current = current
        self.saved = saved
        self.relations = list(relations or [])
        self.current_relations = list(self.relations if current_relations is None else current_relations)
        self.relation_reads = []
        self.grants_sha256 = grants_sha256
        self.loads = 0
        self.committed = saved if committed is None else committed
        self.committed_relations = self.relations if committed_relations is None else committed_relations

    def grant_status(self, *, account_mode="dedicated", expected_grants_sha256=""):
        return {
            "safe": True,
            "account_mode": account_mode,
            "grants_sha256": self.grants_sha256,
        }

    def load_draft_by_id(self, design_id, *, for_update=False, session=None):
        self.loads += 1
        if self.loads >= 3:
            return dict(self.committed)
        return dict(self.current if self.loads == 1 else self.saved)

    def _relations(self, design_id, session, *, for_update=False):
        self.relation_reads.append(for_update)
        if self.loads >= 3:
            return list(self.committed_relations)
        return list(self.current_relations if for_update else self.relations)

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

    def test_legacy_human_signature_cannot_bypass_new_business_review_gate(self):
        self.artifact.pop("business_review")
        self.artifact["transaction_hash"] = transaction_binding_hash(self.artifact)
        self.artifact["approvals"] = {}
        with patch("gxp_editor.approval.approval_secret", return_value="test-secret"):
            for statement in self.artifact["statements"]:
                expires = (datetime.now() + timedelta(minutes=15)).isoformat(timespec="seconds")
                self.artifact["approvals"][statement["id"]] = {
                    "expires_at": expires, "statement_hash": statement_hash(statement),
                    "transaction_hash": self.artifact["transaction_hash"],
                    "signature": approval_signature(self.config, self.artifact, statement, expires),
                }
        save_artifact(self.path, self.artifact)
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())
        with self.patches(repository, session):
            with self.assertRaisesRegex(WriteExecutionError, "旧审批"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(session.executed, [])
        self.assertEqual(repository.loads, 0)
        self.assertEqual(self.state.transaction["state"], "awaiting_approval")

    def save_approved_artifact(self, artifact):
        self.artifact = artifact
        with patch("gxp_editor.approval.approval_secret", return_value="test-secret"):
            for statement in self.artifact["statements"]:
                approve_statement(self.config, self.artifact, statement["id"])
        save_artifact(self.path, self.artifact)
        self.state = FakeState(str(self.path))

    def test_relation_changes_after_approval_block_every_write(self):
        original = relation()
        changed_snapshots = [
            [relation(data='{"external":true}')],
            [relation(lastModificationTime="2026-08-01 00:00:00.000")],
            [],
            [original, relation(id="external-relation", action_data_id="external-node")],
        ]
        for snapshot in changed_snapshots:
            with self.subTest(snapshot=snapshot):
                self.save_approved_artifact(transaction(
                    current_relations=[original], desired_relations={"node-1": '{"planned":true}'},
                ))
                session = FakeSession(affected_rows=1)
                repository = FakeRepository(
                    baseline(), self.saved_row(), self.artifact["expected_relation_rows"],
                    current_relations=snapshot,
                )
                with self.patches(repository, session):
                    with self.assertRaisesRegex(WriteExecutionError, "relations changed after planning"):
                        execute_approved_transaction("transaction-1")
                self.assertEqual(session.executed, [])
                self.assertEqual(repository.relation_reads, [True])
                self.assertEqual(self.state.transaction["state"], "failed_no_write")

    def test_matching_relation_snapshot_can_save_unchanged_or_updated_relations(self):
        original = relation()
        for desired_data, expected_statement_count in ((original["data"], 1), ('{"planned":true}', 2)):
            with self.subTest(desired_data=desired_data):
                self.save_approved_artifact(transaction(
                    current_relations=[original], desired_relations={"node-1": desired_data},
                ))
                session = FakeSession(affected_rows=1)
                repository = FakeRepository(
                    baseline(), self.saved_row(), self.artifact["expected_relation_rows"],
                    current_relations=[original],
                )
                with self.patches(repository, session):
                    result = execute_approved_transaction("transaction-1")
                self.assertEqual(result["state"], "saved")
                self.assertEqual(len(session.executed), expected_statement_count)
                self.assertEqual(repository.relation_reads, [True, False, True])
                self.assertTrue(result["verified"])
                self.assertTrue(result["session_released"])

    def test_missing_prewrite_relation_snapshot_cannot_execute(self):
        artifact = transaction()
        artifact.pop("old_relation_hash")
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        self.save_approved_artifact(artifact)
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())
        with self.patches(repository, session):
            with self.assertRaisesRegex(WriteExecutionError, "relations changed after planning"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(session.executed, [])

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
                "available": True,
                "success": True,
                "production_compatible": False,
                "source_mode": "development_build",
                "reference_manifest_sha256": "a" * 64,
                "warning": "development references only; production unverified",
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
        self.assertEqual(self.state.session_state, "finished")
        self.assertTrue(result["verified"])
        self.assertTrue(result["session_released"])

        stored = load_artifact(self.path)
        self.assertTrue(stored["executed"])
        self.assertTrue(stored["execution_started"])

    def test_changed_committed_draft_keeps_saved_session_for_recovery(self):
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row(), committed=baseline(js_code="external-save"))
        with self.patches(repository, session):
            result = execute_approved_transaction("transaction-1")
        self.assertEqual(self.state.transaction["state"], "saved")
        self.assertEqual(self.state.session_state, "saved")
        self.assertFalse(result["verified"])
        self.assertFalse(result["session_released"])
        self.assertIn("Committed draft hash", result["verification_error"])
        self.assertIsNone(self.state.verification)
        self.assertEqual(len(session.executed), 1)

    def test_changed_committed_relations_keep_saved_session_for_recovery(self):
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row(), committed_relations=[relation()])
        with self.patches(repository, session):
            result = execute_approved_transaction("transaction-1")
        self.assertEqual(self.state.session_state, "saved")
        self.assertFalse(result["session_released"])
        self.assertIn("Committed relation snapshot", result["verification_error"])
        self.assertIsNone(self.state.verification)

    def test_local_closure_failure_retains_committed_save_without_reexecuting(self):
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())
        with self.patches(repository, session), patch.object(
            self.state, "finish_verified_save", side_effect=OSError("local state unavailable"),
        ):
            result = execute_approved_transaction("transaction-1")
        self.assertEqual(self.state.session_state, "saved")
        self.assertEqual(self.state.transaction["state"], "saved")
        self.assertFalse(result["session_released"])
        self.assertEqual(len(session.executed), 1)

    def test_uncertain_commit_never_verifies_or_releases(self):
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())

        @contextlib.contextmanager
        def uncertain_commit(_database):
            yield session
            raise OSError("commit acknowledgement lost")

        with self.patches(repository, session), patch.object(FakeDatabase, "write_transaction", uncertain_commit):
            with self.assertRaisesRegex(OSError, "acknowledgement lost"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(self.state.transaction["state"], "failed")
        self.assertIsNone(self.state.verification)
        stored = load_artifact(self.path)
        self.assertFalse(stored["executed"])
        self.assertTrue(stored["execution_started"])

    def test_post_commit_read_failure_retains_saved_result(self):
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())
        load = repository.load_draft_by_id

        def read(design_id, **kwargs):
            if repository.loads == 2:
                raise OSError("post-commit database read unavailable")
            return load(design_id, **kwargs)

        with self.patches(repository, session), patch.object(repository, "load_draft_by_id", side_effect=read):
            result = execute_approved_transaction("transaction-1")
        self.assertEqual(self.state.transaction["state"], "saved")
        self.assertEqual(self.state.session_state, "saved")
        self.assertFalse(result["verified"])
        self.assertIn("read unavailable", result["verification_error"])
        self.assertEqual(len(session.executed), 1)

    def test_human_save_finishes_real_local_session_before_platform_can_regenerate(self):
        store = StateStore(Path(self.temporary.name) / "state.sqlite3")
        edit = store.create_session(baseline(), "backup", "compiler")
        plan = store.create_plan(edit["id"], "plan.json", "old", "new", "compiler")
        self.artifact.update(session_id=edit["id"], plan_id=plan)
        self.artifact["approvals"] = {}
        self.artifact["transaction_hash"] = transaction_binding_hash(self.artifact)
        self.save_approved_artifact(self.artifact)
        tx = store.create_transaction(edit["id"], plan, "write", str(self.path), transaction_id="transaction-1")
        self.state = store
        database_session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())
        with self.patches(repository, database_session):
            result = execute_approved_transaction(tx)
        self.assertTrue(result["verified"])
        self.assertTrue(result["session_released"])
        self.assertIsNone(store.active_session())
        service = object.__new__(EditorService)
        service.state = store
        # After the success notice the platform may generate different code.
        # Checking this completed save must return its original receipt.
        with patch.object(service, "_components", side_effect=AssertionError("later platform content")):
            self.assertTrue(service.get_write_status(tx)["verified"])
            self.assertEqual(service.verify_saved_draft(edit["id"])["saved_verification"], result["saved_verification"])
            self.assertTrue(service.finish_edit_session(edit["id"])["finished"])
        self.assertEqual(len(database_session.executed), 1)


if __name__ == "__main__":
    unittest.main()
