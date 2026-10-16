from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from tests.helpers import baseline, editor_config, relation, runtime_validation, transaction
from gxp_editor.compiler import CompilerBridge
from gxp_editor.approval import statement_hash, transaction_binding_hash
from gxp_editor.hashing import compiler_binding_payload, sha256_json, sha256_text
from gxp_editor.transaction import RESTORE_RELATION_SQL, TransactionPlanError, build_transaction_artifact, validate_transaction_artifact


class TransactionTests(unittest.TestCase):
    @staticmethod
    def _canvas_transaction(groups, binding_checks=None):
        template = transaction()
        desired = dict(template["desired_design"])
        desired["data"] = json.dumps({"actionData": groups, "globalVariables": []})
        compiler = copy.deepcopy(template["compiler_evidence"])
        compiler["hashes"]["data_sha256"] = sha256_text(desired["data"])
        proof = compiler["event_binding_validation"]
        proof["javascript_group_keys"] = [group["key"] for group in groups if group.get("actionType") == "js"]
        proof["checks"] = list(binding_checks or [])
        proof["binding_node_keys"] = [check["binding_node_key"] for check in proof["checks"]]
        return build_transaction_artifact(transaction_id="evidence-test", session_id="session-test", plan_id="plan-test", kind="write",
            baseline=baseline(), desired_design=desired, current_relations=[], desired_relations={},
            database_safety=template["database_safety"], runtime_validation=template["runtime_validation"], compiler_evidence=compiler)

    @classmethod
    def _binding_transaction(cls):
        groups = [{"key": "main", "actionType": "js", "data": [{"key": "binding", "elementKey": "ComponentEvent", "paramsValue": {"inputParams": {
            "name": {"paramTypes": "componentsVariable", "value": "componentsVariable-button", "dataType": "EformButton"},
            "eventName": "click", "action": {"value": "callback"}}}}]},
            {"key": "callback", "actionType": "js", "data": [], "inputParams": [{"title": "event", "type": "object"}]}]
        check = {"binding_node_key": "binding", "source_group_key": "main", "target_group_key": "callback",
            "component_id": "button", "component_type": "EformButton", "event_name": "click", "input_params": groups[1]["inputParams"],
            "output_params": [], "parameters_declared": True, "hidden_arguments_allowed": False, "signature_status": "catalog_declared"}
        return cls._canvas_transaction(groups, [check])

    def test_native_regeneration_evidence_is_required(self):
        artifact = transaction()
        artifact["compiler_evidence"].pop("generated_code_consistency_verified")
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "consistency evidence is missing"):
            validate_transaction_artifact(artifact)

    def test_event_validation_evidence_cannot_be_omitted(self):
        artifact = transaction()
        artifact["compiler_evidence"].pop("event_binding_validation")
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "binding validation evidence is missing"):
            validate_transaction_artifact(artifact)

    def test_javascript_evidence_must_cover_exactly_all_candidate_groups(self):
        original = self._canvas_transaction([{"key": "main", "actionType": "js", "data": []}])
        for field, value in (("javascript_syntax_checked", False), ("javascript_lexical_scope", "event_callbacks"),
                             ("javascript_group_keys", []), ("javascript_group_keys", ["main", "invented"]),
                             ("javascript_group_keys", ["main", "main"])):
            with self.subTest(field=field, value=value):
                artifact = copy.deepcopy(original)
                artifact["compiler_evidence"]["event_binding_validation"][field] = value
                artifact["transaction_hash"] = transaction_binding_hash(artifact)
                with self.assertRaisesRegex(TransactionPlanError, "JavaScript.*evidence"):
                    validate_transaction_artifact(artifact)

    def test_event_scope_must_be_unique_and_identify_candidate_events(self):
        original = self._binding_transaction()
        for scope in (["binding", "binding"], ["missing"], ["main"], [1], "binding"):
            with self.subTest(scope=scope):
                artifact = copy.deepcopy(original)
                artifact["compiler_evidence"]["event_binding_validation"]["binding_node_keys"] = scope
                artifact["transaction_hash"] = transaction_binding_hash(artifact)
                with self.assertRaisesRegex(TransactionPlanError, "binding validation"):
                    validate_transaction_artifact(artifact)

    def test_event_check_must_match_canvas_component_target_and_parameters(self):
        original = self._binding_transaction()
        for field, value in (("binding_node_key", "other"), ("source_group_key", "other"), ("target_group_key", "other"),
                             ("component_id", "other"), ("component_type", "OtherButton"), ("event_name", "change"),
                             ("input_params", []), ("output_params", [{"title": "other"}]), ("parameters_declared", False),
                             ("hidden_arguments_allowed", True), ("signature_status", "incomplete")):
            with self.subTest(field=field):
                artifact = copy.deepcopy(original)
                artifact["compiler_evidence"]["event_binding_validation"]["checks"][0][field] = value
                artifact["transaction_hash"] = transaction_binding_hash(artifact)
                with self.assertRaisesRegex(TransactionPlanError, "binding.*(check|contract)"):
                    validate_transaction_artifact(artifact)

    def test_actual_compiler_binding_is_accepted_by_approval_validation(self):
        artifact = transaction()
        evidence = artifact["compiler_evidence"]
        bridge = CompilerBridge(editor_config())
        registry = {**evidence["compiler_binding"], "adapter_sha256": "f" * 64}
        with patch.object(bridge, "describe_registry", return_value=registry):
            evidence["compiler_binding"] = bridge.compiler_binding()
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        validate_transaction_artifact(artifact)

    def test_adapter_tampering_is_rejected_even_when_transaction_hash_is_rebound(self):
        artifact = transaction()
        artifact["compiler_evidence"]["compiler_binding"]["adapter_sha256"] = "e" * 64
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "Compiler binding hash is invalid"):
            validate_transaction_artifact(artifact)

    def test_missing_or_invalid_adapter_fingerprint_is_rejected(self):
        for value in (None, "", "invalid", "z" * 64):
            with self.subTest(value=value):
                artifact = transaction()
                binding = artifact["compiler_evidence"]["compiler_binding"]
                if value is None:
                    binding.pop("adapter_sha256")
                else:
                    binding["adapter_sha256"] = value
                binding["compiler_binding_sha256"] = sha256_json(compiler_binding_payload(binding))
                artifact["transaction_hash"] = transaction_binding_hash(artifact)
                with self.assertRaisesRegex(TransactionPlanError, "Compiler binding field adapter_sha256 is invalid"):
                    validate_transaction_artifact(artifact)

    def test_sql_template_tampering_is_rejected(self):
        artifact = transaction()
        statement = artifact["statements"][0]
        statement["sql"] += "; DROP TABLE cpm_bizflows_design"
        statement["sql_sha256"] = sha256_text(statement["sql"])
        statement["statement_hash"] = statement_hash(statement)
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "多条 SQL|approved SQL template"):
            validate_transaction_artifact(artifact)

    def test_relation_cannot_target_another_design(self):
        artifact = transaction(current_relations=[relation()], desired_relations={})
        statement = next(item for item in artifact["statements"] if item["kind"] == "delete_relation")
        statement["params"][-1] = "another-design"
        statement["statement_hash"] = statement_hash(statement)
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "ownership guard|审批目标不一致"):
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

    def test_unavailable_or_failed_csharp_cannot_create_write_approval(self):
        for available in (False, True):
            artifact = transaction()
            artifact["runtime_validation"].update(available=available, success=False,
                source_mode="development_build", production_compatible=False, reference_manifest_sha256="")
            artifact["transaction_hash"] = transaction_binding_hash(artifact)
            with self.assertRaisesRegex(TransactionPlanError, "compilation must succeed"):
                validate_transaction_artifact(artifact)

    def test_runtime_evidence_must_match_candidate_csharp(self):
        artifact = transaction()
        artifact["runtime_validation"]["csharp_sha256"] = "b" * 64
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "desired design"):
            validate_transaction_artifact(artifact)

    def test_invalid_canvas_compiler_evidence_is_rejected(self):
        artifact = transaction()
        artifact["compiler_evidence"]["validation"]["valid"] = False
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "validation is not complete"):
            validate_transaction_artifact(artifact)


if __name__ == "__main__":
    unittest.main()
