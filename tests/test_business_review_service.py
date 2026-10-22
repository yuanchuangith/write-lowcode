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
        self.runner = Mock(side_effect=self.run_pair)
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(self.service, "_components", return_value=(
            editor_config(), object(), self.repository, self.compiler, object())))
        stack.enter_context(patch.object(self.service, "editor_status", return_value={"write_ready": True, "gates": {}}))
        stack.enter_context(patch.object(self.service, "_runtime_validation_evidence", return_value=runtime_validation("")))
        stack.enter_context(patch.object(self.service, "_launch_approval", self.launch))
        stack.enter_context(patch("gxp_editor.service.runtime_root", return_value=self.root))
        stack.enter_context(patch("gxp_editor.service.LogicScenarioRunner.run_pair", self.runner))

    def compile(self, design, operations, *, baseline_artifacts=None):
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
                           "candidate": {"status": self.outcome, "actual": {}, "trace": []}}
                          for case in spec["cases"]]}

    def plan(self, spec=None):
        return self.service.plan_canvas_patch(self.session_id, [], review_spec=spec)

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
        with self.assertRaises((EditorServiceError, LogicReviewError)):
            self.plan()

    def test_removing_failed_review_cannot_downgrade_retry_to_legacy(self):
        self.outcome = "failed"
        result = self.plan(review_spec())
        plan = self.service.state.get_plan(result["plan_id"])
        artifact = load_artifact(plan["artifact_path"])
        del artifact["business_review"]
        save_artifact(plan["artifact_path"], artifact)
        with self.assertRaisesRegex(EditorServiceError, "移除或损坏"):
            self.plan()

    def test_failed_plan_preview_creates_no_transaction_or_approval(self):
        self.outcome = "failed"
        result = self.plan(review_spec())
        with patch("gxp_editor.service.subprocess.Popen") as preview:
            report = self.service.open_plan_review(result["plan_id"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["review_window_opened"])
        args = preview.call_args.args[0]
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
        result = self.service.plan_canvas_patch(self.session_id, [], review_spec=review_spec(), response_detail="summary")
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

    def test_incremental_plan_uses_saved_baseline_and_refuses_concurrent_relations(self):
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
