"""Exercise planning, expectation inheritance, and approval gates without business IO."""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.helpers import baseline, editor_config, runtime_validation
from tests.test_service import FakeCompiler, PlanRepository
from gxp_editor.approval import load_artifact, save_artifact
from gxp_editor.hashing import sha256_json, sha256_text
from gxp_editor.logic_review import LogicReviewError
from gxp_editor.logic_scenarios import RUNNER_VERSION, LogicScenarioRunner
from tests.scenario_evidence import passed_receipt
from gxp_editor.service import EditorService, EditorServiceError
from gxp_editor.state import StateStore


_RUN_PAIR = LogicScenarioRunner.run_pair
_FRONTEND = os.environ.get("GXP_TEST_FRONTEND_REPO", r"G:\hoyi\updateComponents\gxp2.components")


def review_spec():
    return {
        "schema_version": 1, "revision": "request-1",
        "requirements": [{"id": "retain", "text": "编辑时保留原值", "source_text": "仅新增时赋值，编辑要保留原值",
                          "targets": [{"group_key": "main", "node_key": "assign"}]}],
        "scenarios": {"schema_version": 1, "cases": [{
            "id": "edit", "title": "编辑已有记录", "requirement_ids": ["retain"],
            "environment": {"page_mode": "edit", "fields": {"target": {"value": "old"}}},
            "steps": [{"kind": "call_group", "group_key": "main", "args": []}],
            "expect": {"fields": {"target": {"value": "old"}}},
        }]},
    }


class BusinessReviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.canvas = {"actionData": [{"key": "main", "actionType": "js", "data": [
            {"key": "assign", "elementKey": "SetField", "paramsValue": {"value": "old"}, "depth": 0},
        ]}], "globalVariables": []}
        # Keep real operation-shape preflight active while compilation is isolated below.
        self.operations = [{"operation": "replace_params", "action_group_key": "main", "target_node_key": "assign",
                            "input_params": {"value": "new"}, "output_params": {}}]
        self.draft = baseline(data=json.dumps(self.canvas), csharp_code="")
        self.draft["data_json"] = copy.deepcopy(self.canvas)
        self.compiler = FakeCompiler()
        self.compiler.compile_patch = Mock(side_effect=self.compile)
        self.compiler.verify_compiled_result = Mock(side_effect=lambda result, **kwargs: self.compiler_evidence())
        self.compiler.verify_candidate_artifact = Mock(side_effect=lambda *args, **kwargs: self.compiler_evidence())
        self.repository = PlanRepository(self.draft)
        self.service = object.__new__(EditorService)
        self.service.state = StateStore(self.root / "state.sqlite3")
        session = self.service.state.create_session(
            self.draft, "backup-1", self.compiler.compiler_binding()["compiler_binding_sha256"])
        self.session_id = session["id"]
        self.outcome = "passed"
        self.launch = Mock()
        self.launch.return_value = {"window_opened": True, "window_starting": False}
        self.runner = Mock(side_effect=self.run_pair)
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(self.service, "_components", return_value=(
            editor_config(), object(), self.repository, self.compiler, object())))
        stack.enter_context(patch.object(self.service, "editor_status", return_value={"write_ready": True, "gates": {}}))
        stack.enter_context(patch.object(self.service, "_runtime_validation_evidence", return_value=runtime_validation("")))
        stack.enter_context(patch.object(self.service, "_launch_approval", self.launch))
        stack.enter_context(patch("gxp_editor.service.runtime_root", return_value=self.root))
        stack.enter_context(patch("gxp_editor.service.LogicScenarioRunner.run_pair", self.runner))

    def compile(self, design, operations, *, baseline_artifacts=None, generation_mode="full"):
        candidate = copy.deepcopy(self.canvas)
        candidate["actionData"][0]["data"][0]["paramsValue"]["value"] = "new"
        return {"design": candidate, "js_code": "candidate-js", "csharp_code": "", "relation_data": {}, "diff": []}

    def compiler_evidence(self):
        binding = self.compiler.compiler_binding()
        return {"compiler_binding": binding, "validation": {"valid": True,
                    "registry_hash": binding["registry_hash"], "rule_source_sha256": binding["rule_source_sha256"]},
                "event_binding_validation": {"binding_node_keys": [], "checks": [],
                    "javascript_syntax_checked": True, "javascript_lexical_scope": "all_js_groups", "javascript_group_keys": ["main"]},
                "code_generation_complete": True, "generated_code_consistency_verified": True}

    def run_pair(self, old_js, new_js, spec):
        return {"schema_version": 1, "spec_sha256": sha256_json(spec), "runner_version": RUNNER_VERSION,
                "baseline_source_sha256": sha256_text(old_js), "candidate_source_sha256": sha256_text(new_js),
                "browser_verified": False, "runtime_verified": False,
                "cases": [{"id": case["id"], "title": case.get("title", case["id"]),
                           "requirement_ids": case.get("requirement_ids", []), "expected": case.get("expect", {}),
                           "baseline": {"status": "failed", "actual": {}, "trace": []},
                           "candidate": (passed_receipt(case.get("expect", {})) if self.outcome == "passed"
                                         else {"status": self.outcome, "actual": {}, "trace": []})}
                          for case in spec["cases"]]}

    def plan(self, spec=None):
        return self.service.plan_canvas_patch(self.session_id, self.operations, review_spec=spec)

    def test_failed_candidate_is_atomically_blocked_and_returns_readable_report(self):
        ready = self.plan(review_spec())
        self.outcome = "failed"
        failed = self.plan()
        self.assertFalse(failed["ready_for_approval"])
        self.assertEqual(failed["review_status"], "failed")
        self.assertEqual(self.service.state.get_plan(ready["plan_id"])["status"], "superseded")
        self.assertEqual(self.service.state.get_plan(failed["plan_id"])["status"], "blocked")
        self.assertEqual(self.service.state.get_session(self.session_id)["state"], "planned")
        result = self.service.prepare_write(failed["plan_id"])
        self.assertFalse(result["approval_window_opened"])
        self.assertEqual(result["review_report"], failed["review_report"])
        self.assertEqual(self.service.state.session_transactions(self.session_id), [])
        self.launch.assert_not_called()

    @unittest.skipUnless(shutil.which("node") and os.path.isfile(os.path.join(_FRONTEND, "package.json")),
                         "Existing frontend parser and Node required")
    def test_real_runner_evidence_flows_through_plan_approval_and_signature_binding(self):
        runner = LogicScenarioRunner(SimpleNamespace(frontend_repo=_FRONTEND))
        self.runner.side_effect = lambda old, new, spec: _RUN_PAIR(runner, old, new, spec)
        native_result = self.compile(None, [])
        native_result["js_code"] = 'function(inbiz){return {main:async function(){if(inbiz.getPageMode()==="add") inbiz("target").value="new";}}}'
        self.compiler.compile_patch.side_effect = None
        self.compiler.compile_patch.return_value = native_result
        result = self.plan(review_spec())
        self.assertEqual(result["review_report"]["scenarios"]["cases"][0]["candidate"]["status"], "passed")
        self.assertEqual(result["review_report"]["requirements"][0]["status"], "unverified")
        self.assertIn("节点未验证", result["review_report"]["requirements"][0]["reason"])
        pending = self.service.prepare_write(result["plan_id"])
        tx = self.service.state.get_transaction(pending["transaction_id"])
        artifact = load_artifact(tx["artifact_path"])
        self.assertEqual(artifact["business_review"], result["review_report"])
        self.assertFalse(artifact["approvals"])

    def test_omission_inherits_failed_expectations_and_can_retry(self):
        self.outcome = "failed"
        first = self.plan(review_spec())
        self.outcome = "passed"
        second = self.plan()
        self.assertEqual(first["review_report"]["spec"], second["review_report"]["spec"])
        self.assertTrue(second["ready_for_approval"])
        self.assertEqual(self.runner.call_args.args[2], first["review_report"]["spec"]["scenarios"])

    def test_changed_scenario_requires_new_revision(self):
        self.plan(review_spec())
        changed = review_spec()
        changed["scenarios"]["cases"][0]["title"] = "改后的说明"
        with self.assertRaisesRegex(EditorServiceError, "revision"):
            self.plan(changed)

    def test_target_changes_need_new_user_evidence_including_scope_expansion(self):
        original = review_spec()
        self.plan(original)
        for targets in ([{"group_key": "main"}], [{"group_key": "main", "node_key": "other"}], [],
                        [{"group_key": "other", "node_key": "assign"}],
                        [{"group_key": "main", "node_key": "assign", "field_path": "paramsValue"}]):
            with self.subTest(targets=targets):
                changed = copy.deepcopy(original)
                changed["revision"] = "request-2"
                changed["requirements"][0]["targets"] = targets
                if not targets:
                    changed["scope_policy"] = "manual_review"
                with self.assertRaisesRegex(EditorServiceError, "新的用户依据"):
                    self.plan(changed)
        changed["change_source_text"] = "用户补充：只核对这个元件的参数配置"
        self.assertTrue(self.plan(changed)["ready_for_approval"])

    def test_changed_expectation_requires_new_user_basis(self):
        self.plan(review_spec())
        changed = review_spec()
        changed["revision"] = "request-2"
        changed["requirements"][0]["text"] = "编辑时也覆盖"
        with self.assertRaisesRegex(EditorServiceError, "用户依据"):
            self.plan(changed)
        changed["change_source_text"] = "用户补充：编辑时也覆盖"
        self.assertTrue(self.plan(changed)["ready_for_approval"])

    def test_changed_expectation_cannot_reuse_previous_change_source(self):
        first = review_spec()
        first["change_source_text"] = "用户补充：保留编辑值"
        self.plan(first)
        changed = copy.deepcopy(first)
        changed["revision"] = "request-2"
        changed["scenarios"]["cases"][0]["expect"]["fields"]["target"]["value"] = "new"
        for reused_source in (first["change_source_text"], "  " + first["change_source_text"] + "  "):
            with self.subTest(source=reused_source):
                changed["change_source_text"] = reused_source
                with self.assertRaisesRegex(EditorServiceError, "用户依据"):
                    self.plan(changed)
        changed["change_source_text"] = "用户补充：现在编辑时也覆盖"
        self.assertTrue(self.plan(changed)["ready_for_approval"])

    def test_source_text_whitespace_is_not_a_new_user_basis(self):
        self.plan(review_spec())
        changed = review_spec()
        changed["revision"] = "request-2"
        changed["requirements"][0]["text"] = "编辑时也覆盖"
        changed["requirements"][0]["source_text"] = "  " + changed["requirements"][0]["source_text"] + "  "
        with self.assertRaisesRegex(EditorServiceError, "用户依据"):
            self.plan(changed)

    def test_bool_to_number_scenario_change_requires_new_user_basis(self):
        for field in ("environment", "expect"):
            with self.subTest(field=field):
                first = review_spec()
                first["revision"] = "request-" + field
                first["scenarios"]["cases"][0][field]["fields"]["target"]["value"] = True
                if self.service.state.get_session(self.session_id).get("plan_id"):
                    first["change_source_text"] = "用户补充：验证 " + field
                self.plan(first)
                changed = copy.deepcopy(first)
                changed["revision"] += "-changed"
                changed["scenarios"]["cases"][0][field]["fields"]["target"]["value"] = 1
                with self.assertRaisesRegex(EditorServiceError, "用户依据"):
                    self.plan(changed)

    def test_missing_expectations_are_unverified_and_can_enter_human_review(self):
        with self.assertRaisesRegex(LogicReviewError, "每条需求都必须指定 targets"):
            self.plan()
        result = self.plan({"schema_version": 1, "revision": "manual", "scope_policy": "manual_review", "requirements": []})
        self.assertEqual(result["review_status"], "unverified")
        pending = self.service.prepare_write(result["plan_id"])
        self.assertEqual(pending["state"], "awaiting_approval")
        self.assertEqual(pending["review_status"], "unverified")
        self.launch.assert_called_once()
        tx = self.service.state.get_transaction(pending["transaction_id"])
        artifact = load_artifact(tx["artifact_path"])
        self.assertEqual(artifact["business_review"], result["review_report"])
        self.assertFalse(artifact["approvals"])

    def test_cannot_remove_a_failing_case_by_only_changing_revision(self):
        self.outcome = "failed"
        self.plan(review_spec())
        changed = review_spec()
        changed["revision"] = "request-2"
        changed["scenarios"]["cases"] = []
        with self.assertRaisesRegex(EditorServiceError, "用户依据"):
            self.plan(changed)
        changed = review_spec()
        changed["revision"] = "request-2"
        changed["scenarios"]["cases"][0]["expect"]["fields"]["target"]["value"] = "new"
        with self.assertRaisesRegex(EditorServiceError, "用户依据"):
            self.plan(changed)

    def assertion_spec(self, expected="new"):
        spec = review_spec()
        spec["canvas_assertions"] = [{"id": "configured-value", "requirement_ids": ["retain"],
            "target": {"group_key": "main", "node_key": "assign", "element_key": "SetField"},
            "path": "/inputParams/value", "operator": "equals", "value": expected}]
        self.canvas["actionData"][0]["data"][0]["paramsValue"]["inputParams"] = {"value": "new"}
        return spec

    def test_canvas_assertion_failure_blocks_plan_and_omission_inherits_it(self):
        spec = self.assertion_spec("expected")
        result = self.plan(spec)
        self.assertEqual(result["review_status"], "failed")
        self.assertFalse(result["ready_for_approval"])
        self.assertEqual(result["review_report"]["canvas_checks"][0]["status"], "failed")
        inherited = self.plan()
        self.assertEqual(inherited["review_report"]["spec"], result["review_report"]["spec"])
        self.assertFalse(self.service.prepare_write(inherited["plan_id"])["approval_window_opened"])
        self.launch.assert_not_called()

    def test_changing_or_removing_canvas_assertion_needs_revision_and_new_basis(self):
        original = self.assertion_spec()
        self.plan(original)
        changed = copy.deepcopy(original)
        changed["canvas_assertions"][0]["value"] = "other"
        with self.assertRaisesRegex(EditorServiceError, "revision"):
            self.plan(changed)
        changed["revision"] = "request-2"
        with self.assertRaisesRegex(EditorServiceError, "用户依据"):
            self.plan(changed)
        removed = copy.deepcopy(original)
        removed["revision"] = "request-2"
        del removed["canvas_assertions"]
        with self.assertRaisesRegex(EditorServiceError, "用户依据"):
            self.plan(removed)
        changed["change_source_text"] = "用户补充：配置改为 other"
        self.assertEqual(self.plan(changed)["review_status"], "failed")

    def test_relinking_canvas_assertion_needs_new_basis(self):
        original = self.assertion_spec()
        extra = copy.deepcopy(original["requirements"][0])
        extra.update(id="second", text="第二项配置要求")
        original["requirements"].append(extra)
        self.plan(original)
        changed = copy.deepcopy(original)
        changed["revision"] = "request-2"
        changed["canvas_assertions"][0]["requirement_ids"] = ["second"]
        with self.assertRaisesRegex(EditorServiceError, "用户依据"):
            self.plan(changed)

    def test_tampered_candidate_or_report_invalidates_review(self):
        result = self.plan(review_spec())
        plan = self.service.state.get_plan(result["plan_id"])
        artifact = load_artifact(plan["artifact_path"])
        artifact["desired"]["js_code"] += "tampered"
        save_artifact(plan["artifact_path"], artifact)
        with self.assertRaises((EditorServiceError, LogicReviewError)):
            self.service.prepare_write(result["plan_id"])
        self.launch.assert_not_called()
        self.assertEqual(self.service.state.session_transactions(self.session_id), [])

    def test_tampered_report_is_not_inherited(self):
        result = self.plan(review_spec())
        plan = self.service.state.get_plan(result["plan_id"])
        artifact = load_artifact(plan["artifact_path"])
        artifact["business_review"]["spec"]["requirements"] = []
        save_artifact(plan["artifact_path"], artifact)
        retried = self.plan()
        self.assertEqual(retried["review_report"]["spec"], result["review_report"]["spec"])
        self.assertTrue(retried["review_report"]["spec"]["requirements"])
        self.assertEqual(self.service.state.session_transactions(self.session_id), [])

    def test_removing_failed_review_cannot_downgrade_retry_to_legacy(self):
        self.outcome = "failed"
        result = self.plan(review_spec())
        plan = self.service.state.get_plan(result["plan_id"])
        artifact = load_artifact(plan["artifact_path"])
        del artifact["business_review"]
        save_artifact(plan["artifact_path"], artifact)
        retried = self.plan()
        self.assertEqual(retried["review_report"]["spec"], result["review_report"]["spec"])
        self.assertFalse(retried["ready_for_approval"])
        self.assertEqual(self.service.state.session_transactions(self.session_id), [])

    def test_missing_or_broken_candidate_can_replan_from_independent_requirements(self):
        for broken in (None, "{broken"):
            with self.subTest(broken=broken):
                original = self.plan(review_spec())
                path = Path(self.service.state.get_plan(original["plan_id"])["artifact_path"])
                if broken is None:
                    path.unlink()
                else:
                    path.write_text(broken, encoding="utf-8")
                retried = self.plan()
                self.assertEqual(retried["review_report"]["spec"], original["review_report"]["spec"])
                self.assertNotEqual(retried["plan_id"], original["plan_id"])
                self.assertEqual(self.service.state.session_transactions(self.session_id), [])
        self.launch.assert_not_called()

    def test_checkpoint_corruption_cannot_weaken_requirements(self):
        from gxp_editor.state import StateError
        original = self.plan(review_spec())
        with self.service.state._connection() as db:
            db.execute("UPDATE review_spec_checkpoints SET payload_json='{}' WHERE session_id=?", (self.session_id,))
        with self.assertRaisesRegex(StateError, "独立记录损坏"):
            self.plan()
        self.assertEqual(self.service.state.get_session(self.session_id)["plan_id"], original["plan_id"])
        self.assertEqual(self.service.state.session_transactions(self.session_id), [])

    def test_changed_requirements_survive_compile_failure_and_block_old_approval(self):
        original = self.plan(review_spec())
        changed = review_spec()
        changed["revision"] = "request-2"
        changed["requirements"][0]["text"] = "new confirmed requirement"
        changed["change_source_text"] = "user explicitly changed the expected behavior"
        self.compiler.compile_patch.side_effect = RuntimeError("compile failed")
        with self.assertRaisesRegex(RuntimeError, "compile failed"):
            self.plan(changed)
        with self.assertRaisesRegex(EditorServiceError, "已确认要求已变化"):
            self.service.prepare_write(original["plan_id"])
        self.assertEqual(self.service.state.session_transactions(self.session_id), [])
        self.launch.assert_not_called()
        self.compiler.compile_patch.side_effect = self.compile
        retried = self.plan()
        self.assertEqual(retried["review_report"]["spec"]["revision"], "request-2")

    def test_requirements_changed_during_generation_cannot_register_old_candidate(self):
        from gxp_editor.state import StateError
        def mutate(design, operations, **kwargs):
            checkpoint = self.service.state.review_spec_checkpoint(self.session_id)
            changed = copy.deepcopy(checkpoint["spec"])
            changed["revision"] = "concurrent-request"
            self.service.state.save_review_spec_checkpoint(self.session_id, changed, expected_sha256=checkpoint["sha256"])
            return self.compile(design, operations, **kwargs)
        self.compiler.compile_patch.side_effect = mutate
        with self.assertRaisesRegex(StateError, "已确认要求已变化"):
            self.plan(review_spec())
        self.assertIsNone(self.service.state.get_session(self.session_id)["plan_id"])

    def test_legacy_plan_cannot_enter_approval_after_concurrent_new_requirement(self):
        from gxp_editor.state import StateError
        original = self.plan(review_spec())
        with self.service.state._connection() as db:
            db.execute("DELETE FROM review_spec_checkpoints WHERE session_id=?", (self.session_id,))
        def update_during_verification(*args, **kwargs):
            spec = copy.deepcopy(original["review_report"]["spec"])
            spec["revision"] = "new-requirements-failed-to-compile"
            self.service.state.save_review_spec_checkpoint(self.session_id, spec, expected_sha256=None)
            return self.compiler_evidence()
        self.compiler.verify_candidate_artifact.side_effect = update_during_verification
        with self.assertRaisesRegex(StateError, "已确认要求已变化"):
            self.service.prepare_write(original["plan_id"])
        self.assertEqual(self.service.state.session_transactions(self.session_id), [])
        self.launch.assert_not_called()

    def test_legacy_corrupt_candidate_returns_explicit_recovery_path(self):
        original = self.plan(review_spec())
        plan = self.service.state.get_plan(original["plan_id"])
        with self.service.state._connection() as db:
            db.execute("DELETE FROM review_spec_checkpoints WHERE session_id=?", (self.session_id,))
        Path(plan["artifact_path"]).unlink()
        with self.assertRaises(EditorServiceError) as caught:
            self.plan()
        self.assertEqual(caught.exception.details["reason_code"], "legacy_review_spec_unavailable")
        self.assertIn("inspect_session_cancellation", caught.exception.details["next_step"])
        self.assertEqual(self.service.state.session_transactions(self.session_id), [])

    def test_failed_plan_preview_creates_no_transaction_or_approval(self):
        self.outcome = "failed"
        result = self.plan(review_spec())
        with patch("gxp_editor.ui_launch.launch_window", return_value={"window_opened": True}) as preview:
            report = self.service.open_plan_review(result["plan_id"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["review_window_opened"])
        args = preview.call_args.kwargs["arguments"]
        self.assertEqual(args[-2], "--plan-review")
        snapshot = load_artifact(args[-1])
        self.assertEqual(snapshot["business_review"], result["review_report"])
        self.assertEqual(snapshot["before"], self.canvas)
        self.assertEqual(snapshot["action_context"], result["action_context"])
        self.assertEqual(snapshot["action_context"]["design_id"], self.draft["id"])
        self.assertEqual(self.service.state.session_transactions(self.session_id), [])
        self.launch.assert_not_called()

    def test_superseded_plan_cannot_enter_approval(self):
        old = self.plan(review_spec())
        self.plan()
        with self.assertRaisesRegex(EditorServiceError, "current ready"):
            self.service.prepare_write(old["plan_id"])
        self.launch.assert_not_called()

    def test_plan_review_reuses_snapshot_and_waits_for_visible_window_receipt(self):
        result = self.plan(review_spec())
        with patch("gxp_editor.ui_launch.launch_window", return_value={"window_opened": False,
                "window_starting": True, "startup_status": "starting"}) as launch:
            report = self.service.open_plan_review(result["plan_id"])
            first = launch.call_args.kwargs
            snapshot = Path(first["arguments"][-1])
            snapshot.write_text("bad-json", encoding="utf-8")
            self.service.open_plan_review(result["plan_id"])
            self.assertEqual(first["launch_key"], launch.call_args.kwargs["launch_key"])
            self.assertEqual(first["arguments"], launch.call_args.kwargs["arguments"])
            self.assertEqual(load_artifact(snapshot)["business_review"], result["review_report"])
        self.assertFalse(report["review_window_opened"])
        self.assertTrue(report["review_window_starting"])
        self.assertEqual(self.service.state.session_transactions(self.session_id), [])

    def test_legacy_ready_plan_must_be_regenerated(self):
        result = self.plan({"schema_version": 1, "revision": "manual", "scope_policy": "manual_review", "requirements": []})
        plan = self.service.state.get_plan(result["plan_id"])
        artifact = load_artifact(plan["artifact_path"])
        del artifact["business_review"]
        save_artifact(plan["artifact_path"], artifact)
        with self.assertRaisesRegex(EditorServiceError, "重新生成计划"):
            self.service.prepare_write(result["plan_id"])

    def test_summary_response_and_approval_preserve_complete_artifact(self):
        self.outcome = "failed"
        result = self.service.plan_canvas_patch(self.session_id, self.operations, review_spec=review_spec(), response_detail="summary")
        self.assertNotIn("review_report", result)
        self.assertNotIn("diff", result)
        plan = self.service.state.get_plan(result["plan_id"])
        before = Path(plan["artifact_path"]).read_bytes()
        self.assertEqual(load_artifact(plan["artifact_path"])["business_review"]["review_sha256"], result["report_sha256"])
        blocked = self.service.prepare_write(result["plan_id"], response_detail="summary")
        self.assertNotIn("review_report", blocked)
        self.assertFalse(blocked["approval_window_opened"])
        self.assertEqual(Path(plan["artifact_path"]).read_bytes(), before)

    def test_no_change_keeps_prior_plan_and_expectations_explicitly(self):
        prior = self.plan(review_spec())
        self.compiler.compile_patch.side_effect = lambda *args, **kwargs: {"no_change": True}
        self.compiler.verify_compiled_result.reset_mock()
        self.runner.reset_mock()
        response = self.plan()
        self.assertTrue(response["no_changes"])
        self.assertFalse(response["plan_created"])
        self.assertEqual(response["existing_plan_id"], prior["plan_id"])
        self.assertEqual(response["existing_plan_status"], "ready")
        self.assertEqual(self.service.state.get_session(self.session_id)["plan_id"], prior["plan_id"])
        self.compiler.verify_compiled_result.assert_not_called()
        self.runner.assert_not_called()

    def test_plan_uses_saved_baseline_and_refuses_concurrent_relations(self):
        self.plan(review_spec())
        saved = self.compiler.compile_patch.call_args.kwargs["baseline_artifacts"]
        self.assertEqual(saved["js_code"], self.draft["js_code"])
        self.assertEqual(saved["relation_data"], {})
        verified = self.compiler.verify_compiled_result.call_args.kwargs
        self.assertEqual(verified["baseline_artifacts"], saved)
        self.assertIn("actionData", verified["baseline_design"])
        self.draft["relations"] = [{"id": "external", "action_data_id": "assign", "data": "changed"}]
        with self.assertRaisesRegex(EditorServiceError, "relation data changed"):
            self.plan()

    def _assert_generation_mode_roundtrip(self, mode, *, prepare=True):
        requested = mode or "full"
        selected = "incremental" if requested == "incremental" else "full"
        evidence = self.compiler_evidence()
        selection = {"requested_mode": requested, "selected_mode": selected,
            "reason_code": {"full": "explicit_full", "incremental": "explicit_incremental", "auto": "affected_group_ratio"}[requested],
            "policy_version": "action_groups_v1", "baseline_sha256": "a" * 64, "candidate_sha256": "b" * 64,
            "counts": {"total_group_count": 1, "direct_changed_group_count": 1, "affected_group_count": 1,
                       "dependency_group_count": 0, "changed_node_count": 1, "affected_group_ratio": 1,
                       "language_changed_group_count": 0},
            "thresholds": {"max_affected_groups": 3, "max_changed_nodes": 30, "max_affected_group_ratio": 0.5},
            "scope": {"affected_group_keys": ["main"], "unaffected_group_keys": [],
                      "removed_group_keys": [], "shared_context_changed": False}}
        evidence["generation_selection"] = selection
        evidence["event_binding_validation"]["generation_selection"] = copy.deepcopy(selection)
        if selected == "incremental":
            generation = {"mode": "incremental_action_groups", "regenerated_group_keys": ["main"],
                "preserved_group_keys": [], "removed_group_keys": [], "shared_context_changed": False,
                "baseline_sha256": "a" * 64, "candidate_sha256": "b" * 64, "preserved_source_verified": True}
            evidence["generation"] = generation
            evidence["event_binding_validation"]["generation"] = copy.deepcopy(generation)
            evidence["event_binding_validation"]["baseline_diagnostics"] = []
        self.compiler.verify_compiled_result.side_effect = lambda *a, **k: copy.deepcopy(evidence)
        self.compiler.verify_candidate_artifact.side_effect = lambda *a, **k: copy.deepcopy(evidence)
        options = {} if mode is None else {"generation_mode": mode}
        result = self.service.plan_canvas_patch(self.session_id, self.operations, review_spec=review_spec(), **options)
        self.assertEqual(result["generation_selection"], selection)
        self.assertEqual(self.compiler.compile_patch.call_args.kwargs["generation_mode"], requested)
        self.assertEqual(self.compiler.verify_compiled_result.call_args.kwargs["generation_mode"], requested)
        plan = self.service.state.get_plan(result["plan_id"])
        artifact = load_artifact(plan["artifact_path"])
        self.assertEqual(artifact["generation_mode"], requested)
        self.assertEqual(artifact["generation_selection"], selection)
        if not prepare:
            return result, plan, artifact
        pending = self.service.prepare_write(result["plan_id"])
        verification = self.compiler.verify_candidate_artifact.call_args.kwargs
        self.assertEqual(verification["generation_mode"], requested)
        self.assertEqual(verification["baseline_artifacts"]["js_code"], self.draft["js_code"])
        tx = self.service.state.get_transaction(pending["transaction_id"])
        transaction = load_artifact(tx["artifact_path"])
        self.assertEqual(transaction["compiler_evidence"]["generation_selection"], selection)
        self.assertFalse(transaction["approvals"])
        return result, plan, artifact

    def test_default_full_mode_roundtrips_to_approval(self):
        self._assert_generation_mode_roundtrip(None)

    def test_explicit_incremental_mode_roundtrips_to_approval(self):
        self._assert_generation_mode_roundtrip("incremental")

    def test_auto_mode_decision_roundtrips_to_approval(self):
        self._assert_generation_mode_roundtrip("auto")

    def test_invalid_generation_mode_is_rejected_before_compilation(self):
        with self.assertRaisesRegex(EditorServiceError, "generation_mode"):
            self.service.plan_canvas_patch(self.session_id, self.operations, review_spec=review_spec(), generation_mode="invalid")
        self.compiler.compile_patch.assert_not_called()
        self.assertEqual(self.service.state.session_transactions(self.session_id), [])

    def test_full_selection_also_rejects_relation_drift_before_approval(self):
        result, _, _ = self._assert_generation_mode_roundtrip(None, prepare=False)
        self.draft["relations"] = [{"id": "external", "action_data_id": "assign", "data": "changed"}]
        with self.assertRaisesRegex(EditorServiceError, "relation data changed"):
            self.service.prepare_write(result["plan_id"])
        self.compiler.verify_candidate_artifact.assert_not_called()
        self.launch.assert_not_called()
        self.assertEqual(self.service.state.session_transactions(self.session_id), [])

    def test_stored_mode_and_selection_are_bound_to_plan(self):
        result = self.plan(review_spec())
        plan = self.service.state.get_plan(result["plan_id"])
        original = load_artifact(plan["artifact_path"])
        for field, value in (("generation_mode", "incremental"), ("generation_selection", {"selected_mode": "incremental"})):
            with self.subTest(field=field):
                artifact = copy.deepcopy(original)
                artifact[field] = value
                save_artifact(plan["artifact_path"], artifact)
                with self.assertRaises((EditorServiceError, LogicReviewError)):
                    self.service.prepare_write(result["plan_id"])
        self.launch.assert_not_called()
        self.compiler.verify_candidate_artifact.assert_not_called()

    def test_nonempty_saved_relations_are_loaded_separately(self):
        from tests.helpers import relation
        rows = [relation(action_data_id="assign")]
        self.service.state.update_session(self.session_id, "aborted")
        session = self.service.state.create_session({**self.draft, "relations": rows}, "backup-2",
                                                  self.compiler.compiler_binding()["compiler_binding_sha256"])
        with patch.object(self.repository, "load_relations", return_value=rows):
            result = self.service.plan_canvas_patch(session["id"], self.operations, review_spec=review_spec())
        self.assertTrue(result["ready_for_approval"])

    def test_invalid_preview_detail_does_not_open_a_window(self):
        with patch("gxp_editor.service.subprocess.Popen") as launch:
            with self.assertRaisesRegex(EditorServiceError, "response_detail"):
                self.service.open_plan_review("unused", response_detail="invalid")
            launch.assert_not_called()

    def test_switching_to_manual_scope_requires_new_user_basis(self):
        self.plan(review_spec())
        changed = {**review_spec(), "revision": "request-2", "scope_policy": "manual_review"}
        with self.assertRaisesRegex(EditorServiceError, "新的用户依据"):
            self.plan(changed)
        changed["change_source_text"] = "用户确认改为人工检查整个候选范围"
        self.assertTrue(self.plan(changed)["ready_for_approval"])


if __name__ == "__main__":
    unittest.main()
