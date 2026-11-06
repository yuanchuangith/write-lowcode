"""Plan candidate reuse with real temporary state/review artifacts; no business IO."""
from __future__ import annotations

from copy import deepcopy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests import test_business_review_service as review_fixtures
from tests.helpers import runtime_validation
from gxp_editor.approval import load_artifact, save_artifact
from gxp_editor.compiler import CompilerError
from gxp_editor.hashing import sha256_json, sha256_text
from gxp_editor.logic_review import plan_binding_hash
from gxp_editor.planning_reuse import candidate_evidence_hash, reuse_planned_candidate
from gxp_editor.state import StateError


class PlanningReuseTests(unittest.TestCase):
    def setUp(self):
        self.h = review_fixtures.BusinessReviewServiceTests("test_omission_inherits_failed_expectations_and_can_retry")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.runtime = SimpleNamespace(reuse_evidence=Mock(side_effect=lambda evidence, source: {**evidence, "build_reused": True}))
        self.verify_binding = Mock(side_effect=self.h.compiler.verify_binding)
        self.h.compiler.verify_binding = self.verify_binding

    def first_plan(self, *, failed=False):
        self.h.outcome = "failed" if failed else "passed"
        response = self.h.plan(review_fixtures.review_spec())
        self.plan_id = response["plan_id"]
        self.plan = self.h.service.state.get_plan(self.plan_id)
        self.artifact = load_artifact(self.plan["artifact_path"])
        # Older harness service revisions may lack the new commitment; upgrade
        # only this synthetic temporary fixture, never a real legacy plan.
        if "candidate_evidence_hash" not in self.artifact:
            self.artifact["candidate_evidence_hash"] = candidate_evidence_hash(self.artifact)
            self.save_fixture(rebind=True)
        self.session = self.h.service.state.get_session(self.h.session_id)
        return response

    def save_fixture(self, *, rebind=False):
        save_artifact(self.plan["artifact_path"], self.artifact)
        if rebind:
            with self.h.service.state._connection() as database:
                database.execute("UPDATE plans SET new_hash=? WHERE id=?", (plan_binding_hash(self.artifact), self.plan_id))
            self.plan = self.h.service.state.get_plan(self.plan_id)

    def reuse(self, **overrides):
        arguments = {"service": self.h.service, "session": self.session, "current": self.h.draft,
                     "compiler": self.h.compiler, "runtime_validator": self.runtime,
                     "operations": self.h.operations, "generation_mode": "full"}
        arguments.update(overrides)
        return reuse_planned_candidate(**arguments)

    def test_exact_latest_plan_reuses_native_candidate_without_reusing_review_or_approval(self):
        self.first_plan()
        native_calls = self.h.compiler.compile_patch.call_count
        scenario_calls = self.h.runner.call_count
        reused = self.reuse(actual_binding=self.h.compiler.compiler_binding())
        self.assertEqual(self.plan_id, reused["reused_from_plan_id"])
        self.assertEqual(self.artifact["desired"]["js_code"], reused["result"]["js_code"])
        self.assertEqual(self.artifact["event_binding_validation"], reused["compiler_evidence"]["event_binding_validation"])
        self.assertEqual(self.artifact["runtime_validation"], reused["runtime_validation"])
        self.assertNotIn("build_reused", reused["runtime_validation"])
        self.assertNotIn("business_review", reused)
        self.assertNotIn("scenarios", reused)
        self.assertNotIn("approvals", reused)
        self.assertEqual(native_calls, self.h.compiler.compile_patch.call_count)
        self.assertEqual(scenario_calls, self.h.runner.call_count)
        self.verify_binding.assert_called_once_with(self.artifact["compiler_binding"])
        self.runtime.reuse_evidence.assert_called_once()
        reused["result"]["relation_data"]["changed"] = True
        self.assertNotIn("changed", load_artifact(self.plan["artifact_path"])["desired"]["relation_data"])

    def test_blocked_business_review_does_not_discard_complete_native_candidate(self):
        self.first_plan(failed=True)
        self.assertEqual("blocked", self.h.service.state.get_plan(self.plan_id)["status"])
        self.assertIsNotNone(self.reuse())
        self.assertEqual("failed", load_artifact(self.plan["artifact_path"])["business_review"]["status"])

    def test_changed_operations_or_generation_mode_take_normal_generation_path(self):
        self.first_plan()
        changed = deepcopy(self.h.operations)
        changed[0]["input_params"]["value"] = "different"
        for overrides in ({"operations": changed}, {"generation_mode": "incremental"}, {"repaired": {"repairs": []}}):
            with self.subTest(overrides=list(overrides)):
                self.assertIsNone(self.reuse(**overrides))
        self.verify_binding.assert_not_called()
        self.runtime.reuse_evidence.assert_not_called()

    def test_reference_repair_or_no_prior_plan_is_not_reused(self):
        self.first_plan()
        self.assertIsNone(self.reuse(session={**self.session, "session_mode": "reference_repair"}))
        self.assertIsNone(self.reuse(session={**self.session, "state": "locked", "plan_id": None}))

    def test_legacy_plan_without_full_evidence_commitment_is_regenerated(self):
        self.first_plan()
        self.artifact.pop("candidate_evidence_hash")
        self.save_fixture(rebind=True)
        self.assertIsNone(self.reuse())
        self.verify_binding.assert_not_called()

    def test_missing_or_no_longer_reusable_runtime_evidence_falls_back(self):
        self.first_plan()
        self.runtime.reuse_evidence.return_value = None
        self.runtime.reuse_evidence.side_effect = None
        self.assertIsNone(self.reuse())
        self.assertIsNone(self.reuse(runtime_validator=object()))

    def test_old_value_source_baseline_or_actual_binding_drift_is_rejected(self):
        self.first_plan()
        with self.assertRaises(CompilerError):
            self.reuse(current={**self.h.draft, "content_hash": "changed"})
        changed = deepcopy(self.h.compiler.compiler_binding())
        changed["bridge_sha256"] = "changed"
        with self.assertRaises(CompilerError):
            self.reuse(actual_binding=changed)
        self.verify_binding.side_effect = CompilerError("Native source bytes changed")
        with self.assertRaises(CompilerError):
            self.reuse()

    def test_tampering_any_unreviewed_generation_evidence_is_not_a_quiet_cache_miss(self):
        self.first_plan()
        original = deepcopy(self.artifact)
        for field in ("runtime_validation", "validation", "event_binding_validation", "reference_repairs",
                      "interface_metadata", "code_generation_complete", "generated_code_consistency_verified", "desired", "hashes"):
            self.artifact = deepcopy(original)
            value = self.artifact.get(field)
            if isinstance(value, dict):
                value["tampered"] = True
            elif isinstance(value, list):
                value.append({"tampered": True})
            else:
                self.artifact[field] = False if value is True else {"tampered": True}
            self.save_fixture()
            with self.subTest(field=field), self.assertRaises((ValueError, RuntimeError)):
                self.reuse()

    def test_stored_and_actual_desired_hashes_are_independently_bound(self):
        self.first_plan()
        original_hash = candidate_evidence_hash(self.artifact)
        self.artifact["desired"]["js_code"] = "different-native-source"
        self.assertNotEqual(original_hash, candidate_evidence_hash(self.artifact))
        self.artifact["hashes"]["js_sha256"] = sha256_text(self.artifact["desired"]["js_code"])
        self.assertNotEqual(original_hash, candidate_evidence_hash(self.artifact))
        self.artifact["candidate_evidence_hash"] = candidate_evidence_hash(self.artifact)
        self.save_fixture()
        with self.assertRaises((ValueError, RuntimeError)):
            self.reuse()

    def test_ownership_or_latest_plan_changes_during_source_check_are_rejected(self):
        self.first_plan()
        def mutate_source_check(expected):
            self.h.service.state.update_session(self.h.session_id, "finished")
            return self.h.compiler.compiler_binding()
        self.verify_binding.side_effect = mutate_source_check
        with self.assertRaises(StateError):
            self.reuse()

    def test_wrong_runtime_source_receipt_is_rejected(self):
        self.first_plan()
        self.runtime.reuse_evidence.side_effect = lambda evidence, source: {**evidence, "csharp_sha256": "changed"}
        with self.assertRaises(CompilerError):
            self.reuse()

    def test_new_requirements_do_not_change_native_commitment(self):
        self.first_plan()
        before = candidate_evidence_hash(self.artifact)
        changed = deepcopy(self.artifact)
        changed["business_review"] = {"spec": {"revision": "another-review"}, "scenarios": {"cases": []}}
        self.assertEqual(before, candidate_evidence_hash(changed))
        self.assertNotEqual(plan_binding_hash(self.artifact), plan_binding_hash(changed))

    def test_current_runtime_recheck_observations_do_not_change_original_signed_evidence(self):
        self.first_plan()
        original = deepcopy(self.artifact["runtime_validation"])
        before = candidate_evidence_hash(self.artifact)
        self.runtime.reuse_evidence.side_effect = lambda evidence, source: {**evidence, "build_reused": True, "observed_elapsed_ms": 123}
        reused = self.reuse()
        self.runtime.reuse_evidence.assert_called_once_with(original, self.artifact["desired"]["csharp_code"])
        self.assertEqual(original, reused["runtime_validation"])
        self.assertNotIn("build_reused", reused["runtime_validation"])
        self.assertNotIn("observed_elapsed_ms", reused["runtime_validation"])
        candidate = deepcopy(self.artifact)
        candidate["runtime_validation"] = reused["runtime_validation"]
        self.assertEqual(before, candidate_evidence_hash(candidate))

    def test_service_reruns_review_and_scenarios_and_supersedes_blocked_plan_without_recompiling(self):
        self.first_plan(failed=True)
        compile_count = self.h.compiler.compile_patch.call_count
        verification_count = self.h.compiler.verify_compiled_result.call_count
        scenarios_count = self.h.runner.call_count
        runtime_count = self.h.service._runtime_validation_evidence.call_count
        components = self.h.service._components()
        self.h.outcome = "passed"
        new_spec = review_fixtures.review_spec()
        new_spec["revision"] = "request-2"
        new_spec["scenarios"]["cases"][0]["title"] = "Fresh independently rerun review"
        with patch.object(self.h.service, "_components", return_value=(*components[:4], self.runtime)):
            second = self.h.plan(new_spec)
        self.assertTrue(second["ready_for_approval"])
        self.assertEqual(self.plan_id, second["candidate_reused_from_plan_id"])
        self.assertEqual(compile_count, self.h.compiler.compile_patch.call_count)
        self.assertEqual(verification_count, self.h.compiler.verify_compiled_result.call_count)
        self.assertEqual(runtime_count, self.h.service._runtime_validation_evidence.call_count)
        self.assertEqual(scenarios_count + 1, self.h.runner.call_count)
        new_plan = self.h.service.state.get_plan(second["plan_id"])
        new_artifact = load_artifact(new_plan["artifact_path"])
        self.assertNotEqual(self.plan_id, second["plan_id"])
        self.assertEqual("superseded", self.h.service.state.get_plan(self.plan_id)["status"])
        self.assertEqual("request-2", new_artifact["business_review"]["spec"]["revision"])
        self.assertNotEqual(self.artifact["business_review"], new_artifact["business_review"])
        self.assertEqual(self.artifact["desired"], new_artifact["desired"])
        self.assertEqual([], self.h.service.state.session_transactions(self.h.session_id))
        self.assertNotIn("candidate_reused_from_plan_id", new_artifact)
        self.assertEqual(plan_binding_hash(new_artifact), new_plan["new_hash"])


if __name__ == "__main__":
    unittest.main()
