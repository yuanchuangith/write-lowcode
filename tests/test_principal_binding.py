"""Signed authenticated-actor binding with synthetic connections/artifacts."""
from __future__ import annotations

import contextlib
import copy
import unittest
from unittest.mock import patch

from tests.helpers import baseline, transaction
from tests import test_writer as writer_fixtures
from gxp_editor.approval import approve_statement, load_artifact, save_artifact, transaction_binding_hash
from gxp_editor.repository import EditorRepository, RepositoryError
from gxp_editor.handoff_store import environment_scope
from gxp_editor.transaction import TransactionPlanError, principal_verification, validate_transaction_artifact
from gxp_editor.writer import WriteExecutionError, execute_approved_transaction


PRINCIPAL = "synthetic-editor@localhost"
FakeRepository = writer_fixtures.FakeRepository
FakeSession = writer_fixtures.FakeSession


class PrincipalRepositoryTests(unittest.TestCase):
    def test_grants_and_actual_authenticated_principal_are_read_in_one_session(self):
        class Session:
            def __init__(self):
                self.calls = []

            def query(self, sql, *, max_rows):
                self.calls.append((sql, max_rows))
                if sql == "SELECT CURRENT_USER() AS authenticated_principal":
                    return [{"authenticated_principal": PRINCIPAL}]
                if sql == "SHOW GRANTS":
                    return [{"grant": "GRANT USAGE ON *.* TO 'synthetic-editor'@'localhost'"}]
                raise AssertionError("unexpected query")

        class Database:
            def __init__(self):
                self.session = Session()
                self.sessions = 0

            @contextlib.contextmanager
            def read_session(self):
                self.sessions += 1
                yield self.session

        database = Database()
        status = EditorRepository(database).grant_status()
        self.assertEqual(database.sessions, 1)
        self.assertEqual(database.session.calls,
                         [("SELECT CURRENT_USER() AS authenticated_principal", 1), ("SHOW GRANTS", 50)])
        self.assertEqual(status["authenticated_principal"], PRINCIPAL)
        self.assertTrue(status["principal_verified"])
        self.assertNotIn("grants", status)

    def test_missing_ambiguous_malformed_principal_is_unverified(self):
        class Session:
            def __init__(self, rows):
                self.rows = rows

            def query(self, sql, *, max_rows):
                return self.rows

        for rows in ([], [{"other": "value"}], [{"authenticated_principal": ""}],
                     [{"authenticated_principal": "x\nsecret"}], [{"authenticated_principal": 1}],
                     [{"authenticated_principal": PRINCIPAL}] * 2):
            with self.subTest(rows=rows):
                with self.assertRaises(RepositoryError):
                    EditorRepository.authenticated_principal(Session(rows))

    def test_unavailable_actor_preserves_legacy_grants_contract_without_claiming_actor_match(self):
        class Session:
            def query(self, sql, *, max_rows):
                if sql == "SELECT CURRENT_USER() AS authenticated_principal":
                    raise OSError("synthetic unavailable connection observation")
                return [{"grant": "GRANT UPDATE (data, js_code, csharp_code, lastModificationTime) ON synthetic.cpm_bizflows_design TO 'actor'@'localhost'"},
                        {"grant": "GRANT SELECT, INSERT, UPDATE, DELETE ON synthetic.cpm_bizflows_design_relation_data TO 'actor'@'localhost'"}]

        class Database:
            @contextlib.contextmanager
            def read_session(self):
                yield Session()

        status = EditorRepository(Database()).grant_status()
        self.assertTrue(status["safe"])
        self.assertFalse(status["principal_verified"])
        self.assertIsNone(status["authenticated_principal"])
        self.assertEqual(principal_verification({}, status["authenticated_principal"]), "legacy_grants_only")
        with self.assertRaises(TransactionPlanError):
            principal_verification({"authenticated_principal": PRINCIPAL}, status["authenticated_principal"])


class PrincipalArtifactTests(unittest.TestCase):
    def safety(self):
        return {"account_mode": "dedicated", "grants_sha256": "c" * 64,
                "shared_account": False, "privileged_shared_override": False}

    def test_new_actor_is_frozen_inside_existing_signed_database_safety(self):
        safety = {**self.safety(), "authenticated_principal": PRINCIPAL}
        artifact = transaction(database_safety=safety)
        self.assertEqual(artifact["database_safety"]["authenticated_principal"], PRINCIPAL)
        validate_transaction_artifact(artifact)
        original_hash = artifact["transaction_hash"]
        artifact["database_safety"]["authenticated_principal"] = "other-editor@localhost"
        self.assertNotEqual(original_hash, transaction_binding_hash(artifact))
        with self.assertRaisesRegex(TransactionPlanError, "metadata hash"):
            validate_transaction_artifact(artifact)

    def test_historic_artifact_remains_unchanged_and_reports_limited_verification(self):
        artifact = transaction(database_safety=self.safety())
        before = copy.deepcopy(artifact)
        validate_transaction_artifact(artifact)
        self.assertEqual(principal_verification(artifact["database_safety"], PRINCIPAL), "legacy_grants_only")
        self.assertEqual(principal_verification(artifact["database_safety"], None), "legacy_grants_only")
        self.assertEqual(artifact, before)
        self.assertNotIn("authenticated_principal", artifact["database_safety"])

    def test_empty_or_malformed_new_binding_is_not_legacy(self):
        for principal in (None, "", 1, "x" * 513, "actor\n@localhost"):
            with self.subTest(principal=principal):
                with self.assertRaisesRegex(TransactionPlanError, "principal"):
                    transaction(database_safety={**self.safety(), "authenticated_principal": principal})

    def test_optional_target_scope_is_signed_validated_and_legacy_compatible(self):
        artifact = transaction(database_safety={**self.safety(), "database_scope": "a" * 64})
        self.assertEqual(artifact["database_safety"]["database_scope"], "a" * 64)
        original = artifact["transaction_hash"]
        artifact["database_safety"]["database_scope"] = "b" * 64
        self.assertNotEqual(transaction_binding_hash(artifact), original)
        with self.assertRaisesRegex(TransactionPlanError, "scope binding"):
            transaction(database_safety={**self.safety(), "database_scope": "invalid"})

    def test_principal_comparison_preserves_exact_actor_and_supports_unicode(self):
        for principal in (PRINCIPAL, "业务账户@localhost"):
            binding = {"authenticated_principal": principal}
            self.assertEqual(principal_verification(binding, principal), "bound_principal_match")
            for changed in (principal.upper(), principal + " ", "other@localhost"):
                with self.subTest(principal=principal, changed=changed):
                    with self.assertRaisesRegex(TransactionPlanError, "changed"):
                        principal_verification(binding, changed)
        with self.assertRaisesRegex(TransactionPlanError, "unavailable"):
            principal_verification({"authenticated_principal": PRINCIPAL}, None)


class PrincipalWriterTests(unittest.TestCase):
    setUp = writer_fixtures.WriterTests.setUp
    saved_row = writer_fixtures.WriterTests.saved_row
    patches = writer_fixtures.WriterTests.patches

    def bind_principal(self):
        self.artifact["database_safety"]["authenticated_principal"] = PRINCIPAL
        self.artifact["transaction_hash"] = transaction_binding_hash(self.artifact)
        self.artifact["approvals"] = {}
        with patch("gxp_editor.approval.approval_secret", return_value="test-secret"):
            for statement in self.artifact["statements"]:
                approve_statement(self.config, self.artifact, statement["id"])
        save_artifact(self.path, self.artifact)

    def test_same_grants_on_another_account_do_not_authorize_execution(self):
        self.bind_principal()
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row(), authenticated_principal="other-editor@localhost")
        with self.patches(repository, session):
            with self.assertRaisesRegex(WriteExecutionError, "principal changed"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(session.executed, [])
        self.assertEqual(repository.loads, 0)
        self.assertEqual(self.state.session_state, "failed_no_write")

    def test_actual_writer_connection_is_also_checked_before_dml(self):
        self.bind_principal()
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row(), writer_principal="writer-drift@localhost")
        with self.patches(repository, session):
            with self.assertRaisesRegex(WriteExecutionError, "principal changed"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(session.executed, [])
        self.assertEqual(repository.loads, 0)
        self.assertEqual(self.state.session_state, "failed_no_write")

    def test_unreadable_current_actor_does_not_execute_or_claim_legacy(self):
        self.bind_principal()
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row(), authenticated_principal="")
        with self.patches(repository, session):
            with self.assertRaisesRegex(WriteExecutionError, "principal evidence"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(session.executed, [])

    def test_successful_bound_actor_has_explicit_commit_evidence(self):
        self.bind_principal()
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())
        with self.patches(repository, session):
            result = execute_approved_transaction("transaction-1")
        self.assertTrue(result["verified"])
        self.assertEqual(result["principal_verification"], "bound_principal_match")
        self.assertTrue(self.state.verification["commit_confirmed"])
        self.assertTrue(load_artifact(self.path)["executed"])

    def test_legacy_grants_path_is_not_silently_upgraded(self):
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row(), authenticated_principal="different-actor@localhost")
        with self.patches(repository, session):
            result = execute_approved_transaction("transaction-1")
        self.assertTrue(result["verified"])
        self.assertEqual(result["principal_verification"], "legacy_grants_only")
        self.assertNotIn("authenticated_principal", load_artifact(self.path)["database_safety"])

    def test_signed_target_scope_drift_is_rejected_before_claim_or_database_calls(self):
        self.artifact["database_safety"]["database_scope"] = "0" * 64
        self.bind_principal()
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())
        with self.patches(repository, session):
            with self.assertRaisesRegex(WriteExecutionError, "scope changed"):
                execute_approved_transaction("transaction-1")
        self.assertEqual(session.executed, [])
        self.assertEqual(repository.loads, 0)
        self.assertEqual(self.state.transaction["state"], "awaiting_approval")
        self.assertFalse(load_artifact(self.path)["execution_started"])

    def test_signed_target_scope_match_uses_original_bound_value(self):
        self.artifact["database_safety"]["database_scope"] = environment_scope(self.config.database)
        self.bind_principal()
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())
        with self.patches(repository, session):
            result = execute_approved_transaction("transaction-1")
        self.assertTrue(result["executed"])
        self.assertEqual(result["execution_outcome"], "confirmed")

    def test_post_commit_publication_change_retains_commit_but_refuses_draft_verification(self):
        self.bind_principal()
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row(), committed={**self.saved_row(), "is_publish": 1})
        with self.patches(repository, session):
            result = execute_approved_transaction("transaction-1")
        self.assertTrue(result["executed"])
        self.assertFalse(result["verified"])
        self.assertFalse(result["session_released"])
        self.assertIn("unpublished draft", result["verification_error"])
        self.assertEqual(len(session.executed), 1)

    def test_source_drift_after_preflight_is_rejected_before_execution_claim(self):
        self.bind_principal()
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())
        changed = {**self.artifact["compiler_evidence"]["compiler_binding"], "compiler_binding_sha256": "0" * 64}
        with self.patches(repository, session), patch("gxp_editor.writer.CompilerBridge.compiler_binding", return_value=changed):
            with self.assertRaisesRegex(WriteExecutionError, "Compiler binding changed") as caught:
                execute_approved_transaction("transaction-1")
        self.assertEqual(caught.exception.code, "compiler_binding_changed")
        self.assertFalse(caught.exception.details["execution_claimed"])
        self.assertEqual(self.state.transaction["state"], "awaiting_approval")
        self.assertFalse(load_artifact(self.path)["execution_started"])
        self.assertEqual(session.executed, [])
        self.assertEqual(repository.loads, 0)

    def test_unavailable_source_check_is_not_claimed_or_written(self):
        from gxp_editor.compiler import CompilerError
        self.bind_principal()
        session = FakeSession(affected_rows=1)
        repository = FakeRepository(baseline(), self.saved_row())
        with self.patches(repository, session), patch("gxp_editor.writer.CompilerBridge.compiler_binding", side_effect=CompilerError("synthetic source unavailable")):
            with self.assertRaises(CompilerError):
                execute_approved_transaction("transaction-1")
        self.assertEqual(self.state.transaction["state"], "awaiting_approval")
        self.assertFalse(load_artifact(self.path)["execution_started"])
        self.assertEqual(session.executed, [])


if __name__ == "__main__":
    unittest.main()
