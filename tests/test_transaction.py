from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from tests.helpers import baseline, editor_config, relation, runtime_validation, transaction
from gxp_editor.compiler import CompilerBridge
from gxp_editor.approval import statement_hash, transaction_binding_hash
from gxp_editor.hashing import compiler_binding_payload, normalized_relation_rows, relation_snapshot_hash, sha256_json, sha256_text
from gxp_editor.transaction import RESTORE_RELATION_SQL, UPDATE_DESIGN_SQL, UPDATE_RELATION_SQL, TransactionPlanError, build_transaction_artifact, validate_transaction_artifact


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

    @classmethod
    def _generation_selection_transaction(cls, requested="full", selected=None):
        selected = requested if selected is None else selected
        artifact = cls._canvas_transaction([{"key": f"g{i}", "actionType": "js", "data": []} for i in range(6)])
        selection = {"requested_mode": requested, "selected_mode": selected,
            "reason_code": "small_change" if requested == "auto" else "explicit_" + requested,
            "policy_version": "action_groups_v1",
            "counts": {"total_group_count": 6, "direct_changed_group_count": 1, "affected_group_count": 1,
                       "dependency_group_count": 0, "changed_node_count": 1, "affected_group_ratio": 1 / 6,
                       "language_changed_group_count": 0},
            "thresholds": {"max_affected_groups": 3, "max_changed_nodes": 30, "max_affected_group_ratio": 0.5},
            "scope": {"affected_group_keys": ["g0"], "unaffected_group_keys": [f"g{i}" for i in range(1, 6)],
                      "removed_group_keys": [], "shared_context_changed": False},
            "baseline_sha256": "a" * 64, "candidate_sha256": "b" * 64}
        evidence = artifact["compiler_evidence"]
        evidence["generation_selection"] = selection
        proof = evidence["event_binding_validation"]
        proof["generation_selection"] = copy.deepcopy(selection)
        if selected == "incremental":
            evidence["generation"] = {"mode": "incremental_action_groups", "regenerated_group_keys": ["g0"],
                "preserved_group_keys": [f"g{i}" for i in range(1, 6)], "removed_group_keys": [],
                "shared_context_changed": False, "preserved_source_verified": True,
                "baseline_sha256": "a" * 64, "candidate_sha256": "b" * 64}
            proof["generation"] = copy.deepcopy(evidence["generation"])
            proof["baseline_diagnostics"] = []
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        return artifact

    def test_generation_selection_is_optional_for_old_artifacts_and_valid_for_both_modes(self):
        validate_transaction_artifact(transaction())
        for requested, selected in (("full", "full"), ("incremental", "incremental"), ("auto", "incremental")):
            with self.subTest(requested=requested, selected=selected):
                validate_transaction_artifact(self._generation_selection_transaction(requested, selected))

    def test_generation_selection_must_match_event_evidence(self):
        for target in ("compiler_evidence", "event_binding_validation"):
            artifact = self._generation_selection_transaction()
            owner = artifact["compiler_evidence"]
            if target == "event_binding_validation":
                owner = owner[target]
            owner["generation_selection"]["selected_mode"] = "incremental"
            artifact["transaction_hash"] = transaction_binding_hash(artifact)
            with self.subTest(target=target), self.assertRaisesRegex(TransactionPlanError, "Generation selection evidence changed"):
                validate_transaction_artifact(artifact)

    def test_generation_selection_structural_tampering_is_blocked_after_rebinding(self):
        original = self._generation_selection_transaction()
        changes = [
            (("requested_mode",), "fast"), (("selected_mode",), ["full"]), (("policy_version",), "unknown"),
            (("baseline_sha256",), "invalid"), (("candidate_sha256",), "g" * 64),
            (("thresholds", "max_affected_groups"), 100),
            (("counts", "changed_node_count"), True), (("counts", "direct_changed_group_count"), -1),
            (("counts", "affected_group_ratio"), float("nan")), (("counts", "affected_group_ratio"), 0.5),
            (("counts", "total_group_count"), 7), (("counts", "affected_group_count"), 2),
            (("counts", "dependency_group_count"), 1), (("counts", "language_changed_group_count"), 2),
            (("scope", "affected_group_keys"), ["g0", "g0"]),
            (("scope", "unaffected_group_keys"), ["g0", "g2", "g3", "g4", "g5"]),
            (("scope", "removed_group_keys"), ["g1"]), (("scope", "shared_context_changed"), "false"),
            (("scope", "shared_context_changed"), True), (("reason_code",), "small_change")]
        for path, value in changes:
            artifact = copy.deepcopy(original)
            evidence = artifact["compiler_evidence"]
            for selection in (evidence["generation_selection"], evidence["event_binding_validation"]["generation_selection"]):
                owner = selection
                for key in path[:-1]:
                    owner = owner[key]
                owner[path[-1]] = value
            artifact["transaction_hash"] = transaction_binding_hash(artifact)
            with self.subTest(path=path, value=value), self.assertRaisesRegex(TransactionPlanError, "Generation selection"):
                validate_transaction_artifact(artifact)

    def test_generation_selection_cannot_misstate_full_or_incremental_preservation(self):
        for requested, selected in (("full", "incremental"), ("incremental", "full")):
            artifact = self._generation_selection_transaction(requested, selected)
            artifact["transaction_hash"] = transaction_binding_hash(artifact)
            with self.subTest(requested=requested), self.assertRaisesRegex(TransactionPlanError, "explicit requested mode"):
                validate_transaction_artifact(artifact)
        full = self._generation_selection_transaction()
        inc = self._generation_selection_transaction("incremental")
        for target in (full["compiler_evidence"], full["compiler_evidence"]["event_binding_validation"]):
            target["generation"] = copy.deepcopy(inc["compiler_evidence"]["generation"])
        full["compiler_evidence"]["event_binding_validation"]["baseline_diagnostics"] = []
        full["transaction_hash"] = transaction_binding_hash(full)
        with self.assertRaisesRegex(TransactionPlanError, "Full generation selection cannot claim"):
            validate_transaction_artifact(full)
        for target in (inc["compiler_evidence"], inc["compiler_evidence"]["event_binding_validation"]):
            target.pop("generation")
        inc["transaction_hash"] = transaction_binding_hash(inc)
        with self.assertRaisesRegex(TransactionPlanError, "requires preservation evidence"):
            validate_transaction_artifact(inc)

    def test_incremental_selection_hashes_and_group_scope_must_match_preservation(self):
        for field, value in (("baseline_sha256", "c" * 64), ("candidate_sha256", "c" * 64),
                             ("regenerated_group_keys", ["g1"]), ("removed_group_keys", ["old"]),
                             ("shared_context_changed", True)):
            artifact = self._generation_selection_transaction("incremental")
            for owner in (artifact["compiler_evidence"], artifact["compiler_evidence"]["event_binding_validation"]):
                owner["generation"][field] = value
                if field == "regenerated_group_keys":
                    owner["generation"]["preserved_group_keys"] = ["g0", "g2", "g3", "g4", "g5"]
            artifact["transaction_hash"] = transaction_binding_hash(artifact)
            with self.subTest(field=field), self.assertRaisesRegex(TransactionPlanError, "preservation evidence disagree"):
                validate_transaction_artifact(artifact)

    def test_automatic_generation_selection_must_follow_recorded_size_and_reason(self):
        for changed_nodes, reason, selected in ((31, "small_change", "incremental"), (1, "changed_node_limit", "full"),
                                                (1, "baseline_artifacts_unavailable", "incremental")):
            artifact = self._generation_selection_transaction("auto", selected)
            for selection in (artifact["compiler_evidence"]["generation_selection"],
                              artifact["compiler_evidence"]["event_binding_validation"]["generation_selection"]):
                selection["counts"]["changed_node_count"] = changed_nodes
                selection["reason_code"] = reason
            artifact["transaction_hash"] = transaction_binding_hash(artifact)
            with self.subTest(changed_nodes=changed_nodes, reason=reason), self.assertRaisesRegex(TransactionPlanError, "Generation selection"):
                validate_transaction_artifact(artifact)
        valid = self._generation_selection_transaction("auto", "full")
        for selection in (valid["compiler_evidence"]["generation_selection"],
                          valid["compiler_evidence"]["event_binding_validation"]["generation_selection"]):
            selection["counts"]["changed_node_count"] = 31
            selection["reason_code"] = "changed_node_limit"
        valid["transaction_hash"] = transaction_binding_hash(valid)
        validate_transaction_artifact(valid)

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

    def test_unchanged_relation_preserves_metadata_and_snapshot_without_update(self):
        current = relation()
        original = copy.deepcopy(current)
        with patch("gxp_editor.transaction._now", return_value="2026-10-20 10:00:00.123"):
            artifact = transaction(current_relations=[current], desired_relations={"node-1": current["data"]})

        self.assertEqual([item["kind"] for item in artifact["statements"]], ["update_design"])
        self.assertEqual(artifact["expected_relation_rows"], normalized_relation_rows([original]))
        self.assertEqual(artifact["old_relation_hash"], relation_snapshot_hash([original]))
        self.assertEqual(artifact["expected_relation_hash"], artifact["old_relation_hash"])
        self.assertEqual(current, original)

    def test_multiple_canvas_and_both_language_changes_share_one_draft_update(self):
        current = relation()
        template = transaction(current_relations=[current], desired_relations={"node-1": current["data"]})
        groups = [{"key": language, "actionType": language, "data": [
            {"key": f"{language}-{index}", "elementKey": "SetVariable",
             "paramsValue": {"inputParams": {"variableValue": "new"}}} for index in range(2)]}
            for language in ("js", "csharp")]
        old_groups = copy.deepcopy(groups)
        for group in old_groups:
            for node in group["data"]:
                node["paramsValue"]["inputParams"]["variableValue"] = "old"
        base = baseline(data=json.dumps({"actionData": old_groups, "globalVariables": []}))
        desired = {**template["desired_design"], "data": json.dumps({"actionData": groups, "globalVariables": []})}
        compiler = copy.deepcopy(template["compiler_evidence"])
        compiler["hashes"]["data_sha256"] = sha256_text(desired["data"])
        compiler["event_binding_validation"]["javascript_group_keys"] = ["js"]
        artifact = build_transaction_artifact(transaction_id="combined-draft", session_id="session", plan_id="plan", kind="write",
            baseline=base, desired_design=desired, current_relations=[current], desired_relations={"node-1": current["data"]},
            database_safety=template["database_safety"], runtime_validation=template["runtime_validation"], compiler_evidence=compiler)

        self.assertNotEqual(desired["js_code"], base["js_code"])
        self.assertNotEqual(desired["csharp_code"], base["csharp_code"])
        self.assertEqual([item["kind"] for item in artifact["statements"]], ["update_design"])
        statement = artifact["statements"][0]
        self.assertEqual(statement["sql"], UPDATE_DESIGN_SQL)
        self.assertEqual(statement["params"][:3], [desired["data"], desired["js_code"], desired["csharp_code"]])
        self.assertEqual(statement["params"][4], base["id"])
        self.assertEqual(artifact["expected_relation_hash"], artifact["old_relation_hash"])

    def test_different_relation_rows_have_distinct_parameter_bound_approvals(self):
        rows = [relation(id=f"relation-{index}", action_data_id=f"node-{index}") for index in (1, 2)]
        artifact = transaction(current_relations=rows, desired_relations={"node-1": '{"new":true}', "node-2": '{"new":true}'})
        self.assertEqual([item["kind"] for item in artifact["statements"]], ["update_design", "update_relation", "update_relation"])
        first, second = artifact["statements"][1:]
        self.assertEqual(first["sql"], UPDATE_RELATION_SQL)
        self.assertEqual(first["sql"], second["sql"])
        self.assertEqual(first["sql_sha256"], second["sql_sha256"])
        self.assertEqual(first["params"][-2:], ["relation-1", "design-1"])
        self.assertEqual(second["params"][-2:], ["relation-2", "design-1"])
        self.assertNotEqual(first["params"], second["params"])
        self.assertNotEqual(first["statement_hash"], second["statement_hash"])
        # Different row parameters require distinct approvals even if the
        # statement identity is held equal for this hash comparison.
        self.assertNotEqual(statement_hash(first), statement_hash({**second, "id": first["id"]}))

    def test_changed_relation_data_updates_timestamp_and_expected_snapshot(self):
        current = relation()
        expected = {**current, "data": '{"new":true}', "lastModificationTime": "2026-10-20 10:00:00.123"}
        with patch("gxp_editor.transaction._now", return_value=expected["lastModificationTime"]):
            artifact = transaction(current_relations=[current], desired_relations={"node-1": expected["data"]})

        updates = [item for item in artifact["statements"] if item["kind"] == "update_relation"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["sql"], UPDATE_RELATION_SQL)
        self.assertEqual(updates[0]["params"], [expected["data"], current["lastModifierId"],
                         expected["lastModificationTime"], current["id"], current["design_id"]])
        self.assertEqual(artifact["expected_relation_rows"], normalized_relation_rows([expected]))
        self.assertEqual(artifact["expected_relation_hash"], relation_snapshot_hash([expected]))
        self.assertNotEqual(artifact["expected_relation_hash"], artifact["old_relation_hash"])
        self.assertEqual(current["lastModificationTime"], "2026-07-25 08:00:00.000")

    def test_deleted_relation_is_reactivated_even_when_data_is_unchanged(self):
        current = relation(isDeleted=1)
        expected = {**current, "isDeleted": 0, "lastModificationTime": "2026-10-20 10:00:00.123"}
        with patch("gxp_editor.transaction._now", return_value=expected["lastModificationTime"]):
            artifact = transaction(current_relations=[current], desired_relations={"node-1": current["data"]})

        updates = [item for item in artifact["statements"] if item["kind"] == "update_relation"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["sql"], UPDATE_RELATION_SQL)
        self.assertIn("isDeleted=0", updates[0]["sql"])
        self.assertEqual(updates[0]["params"], [current["data"], current["lastModifierId"],
                         expected["lastModificationTime"], current["id"], current["design_id"]])
        self.assertEqual(artifact["expected_relation_rows"], normalized_relation_rows([expected]))
        self.assertEqual(artifact["expected_relation_hash"], relation_snapshot_hash([expected]))
        self.assertNotEqual(artifact["expected_relation_hash"], artifact["old_relation_hash"])
        self.assertEqual(current["isDeleted"], 1)

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
