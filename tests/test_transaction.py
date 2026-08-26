from __future__ import annotations

import copy
import unittest

from tests.helpers import baseline, relation, runtime_validation, transaction
from gxp_editor.approval import statement_hash, transaction_binding_hash
from gxp_editor.hashing import sha256_text
from gxp_editor.transaction import RESTORE_RELATION_SQL, TransactionPlanError, build_transaction_artifact, validate_transaction_artifact


class TransactionTests(unittest.TestCase):
    def test_sql_template_tampering_is_rejected(self):
        artifact = transaction()
        statement = artifact["statements"][0]
        statement["sql"] += "; DROP TABLE cpm_bizflows_design"
        statement["sql_sha256"] = sha256_text(statement["sql"])
        statement["statement_hash"] = statement_hash(statement)
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "approved SQL template"):
            validate_transaction_artifact(artifact)

    def test_relation_cannot_target_another_design(self):
        artifact = transaction(current_relations=[relation()], desired_relations={})
        statement = next(item for item in artifact["statements"] if item["kind"] == "delete_relation")
        statement["params"][-1] = "another-design"
        statement["statement_hash"] = statement_hash(statement)
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "ownership guard"):
            validate_transaction_artifact(artifact)

    def test_restore_preserves_complete_relation_metadata(self):
        current = relation(data='{"current":true}', isDeleted=0)
        original = relation(
            data='{"original":true}',
            isDeleted=1,
            creatorId="original-creator",
            creationTime="2025-01-02 03:04:05.006",
            department="original-dept",
            lastModifierId="original-modifier",
            lastModificationTime="2025-02-03 04:05:06.007",
            org_id="original-org",
            org_name="Original org",
            action_data_id="original-node",
        )
        base = baseline()
        artifact = build_transaction_artifact(
            transaction_id="restore-1",
            session_id="session-1",
            plan_id=None,
            kind="restore",
            baseline=base,
            desired_design={
                "data": base["data"],
                "js_code": base["js_code"],
                "csharp_code": base["csharp_code"],
                "last_modification_time": "2025-02-03 04:05:06.007",
            },
            current_relations=[current],
            desired_relations=[original],
            database_safety={
                "account_mode": "dedicated",
                "grants_sha256": "c" * 64,
                "shared_account": False,
            },
            runtime_validation=runtime_validation(base["csharp_code"]),
        )
        statement = next(item for item in artifact["statements"] if item["kind"] == "restore_relation")
        self.assertEqual(statement["sql"], RESTORE_RELATION_SQL)
        self.assertEqual(
            statement["params"][:10],
            [
                1,
                "original-creator",
                "2025-01-02 03:04:05.006",
                "original-dept",
                "original-modifier",
                "2025-02-03 04:05:06.007",
                "original-org",
                "Original org",
                "original-node",
                '{"original":true}',
            ],
        )

    def test_missing_design_identity_is_rejected(self):
        artifact = transaction()
        artifact["design_id"] = ""
        with self.assertRaisesRegex(TransactionPlanError, "missing its identity"):
            validate_transaction_artifact(artifact)

    def test_execution_state_must_be_boolean(self):
        artifact = transaction()
        artifact["execution_started"] = "yes"
        with self.assertRaisesRegex(TransactionPlanError, "execution state"):
            validate_transaction_artifact(artifact)

    def test_runtime_reference_manifest_is_bound_to_transaction(self):
        artifact = transaction()
        artifact["runtime_validation"]["reference_manifest_sha256"] = "b" * 64
        with self.assertRaisesRegex(TransactionPlanError, "metadata hash"):
            validate_transaction_artifact(artifact)

    def test_database_safety_binding_is_required(self):
        artifact = transaction()
        artifact["database_safety"]["grants_sha256"] = "invalid"
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "grants fingerprint"):
            validate_transaction_artifact(artifact)

    def test_privileged_shared_account_mode_is_bound_to_transaction(self):
        artifact = transaction(
            database_safety={
                "account_mode": "shared_unrestricted",
                "grants_sha256": "c" * 64,
                "shared_account": True,
                "privileged_shared_override": True,
            }
        )
        self.assertTrue(artifact["database_safety"]["privileged_shared_override"])
        validate_transaction_artifact(artifact)

        artifact["database_safety"]["privileged_shared_override"] = False
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "privileged shared-account"):
            validate_transaction_artifact(artifact)

    def test_development_build_runtime_evidence_is_advisory(self):
        artifact = transaction()
        artifact["runtime_validation"].update(
            {
                "available": False,
                "success": False,
                "source_mode": "development_build",
                "production_compatible": False,
                "reference_manifest_sha256": "",
            }
        )
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        validate_transaction_artifact(artifact)

    def test_runtime_evidence_must_match_candidate_csharp(self):
        artifact = transaction()
        artifact["runtime_validation"]["csharp_sha256"] = "b" * 64
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "desired design"):
            validate_transaction_artifact(artifact)


if __name__ == "__main__":
    unittest.main()
