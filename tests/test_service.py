from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import baseline, editor_config
from gxp_editor.compiler import CompilerError
from gxp_editor.hashing import sha256_json
from gxp_editor.logic_review import build_business_review, plan_binding_hash
from gxp_editor.service import EditorService


class FakeState:
    def active_session(self, conversation_id=""):
        return None


class FakeDatabase:
    def status(self):
        return {"connected": True}


class FakeRepository:
    def index_status(self):
        return {"draft": {"ready": True}, "relations": {"ready": True}}

    def grant_status(self, *, account_mode, expected_grants_sha256):
        return {
            "safe": True,
            "dedicated_safe": True,
            "shared_account_warning": False,
            "privileged_shared_override": False,
            "fingerprint_configured": True,
            "fingerprint_matches": True,
            "grants_sha256": "c" * 64,
        }


class FakeCompiler:
    def compiler_binding(self):
        payload = {
            "bridge_sha256": "a" * 64,
            "adapter_sha256": "f" * 64,
            "registry_hash": "b" * 64,
            "rule_source_sha256": "c" * 64,
            "frontend_version": "test",
            "source_provenance": {
                "ready": True,
                "frontend_repo": "test",
                "commit": "d" * 40,
                "branch": "test",
                "dirty": False,
                "dirty_file_count": 0,
                "workspace_sha256": "e" * 64,
                "bridge_path": "bridge.js",
            },
        }
        return {**payload, "compiler_binding_sha256": sha256_json(payload)}

    def describe_registry(self):
        return {
            **self.compiler_binding(),
            "elements": [{"key": "Start"}],
        }


class UnavailableRuntimeValidator:
    def __init__(self, runtime):
        self.runtime = runtime

    def status(self):
        raise RuntimeError("runtime references unavailable")


class RecordingPlanState:
    def __init__(self, session, plan=None):
        self.session = session
        self.plan = plan
        self.plan_created = False
        self.transaction_created = False

    def get_session(self, session_id):
        return dict(self.session)

    def create_plan(self, *args, **kwargs):
        self.plan_created = True
        return "plan-1"

    def get_plan(self, plan_id):
        return dict(self.plan)

    def create_transaction(self, *args, **kwargs):
        self.transaction_created = True
        return "transaction-1"


class PlanRepository(FakeRepository):
    def __init__(self, draft):
        self.draft = draft

    def load_draft_by_id(self, design_id):
        return dict(self.draft)

    def load_relations(self, design_id):
        return []


class BlockingPlanCompiler(FakeCompiler):
    def compile_patch(self, design, operations, *, baseline_artifacts=None):
        return {"design": design}

    def verify_compiled_result(self, result, **kwargs):
        raise CompilerError("Compiler generated no JavaScript")


class BlockingPrepareCompiler(FakeCompiler):
    def verify_candidate_artifact(self, design, desired, **kwargs):
        raise CompilerError("Candidate canvas is invalid")


class ServiceStatusTests(unittest.TestCase):
    def test_saved_session_also_offers_recovery_window(self):
        from unittest.mock import Mock
        service = object.__new__(EditorService)
        service.state = Mock()
        service.state.active_session.return_value = {"id": "saved-session", "state": "saved"}
        with patch.object(service, "abort_edit_session", return_value={"confirmation_window_opened": True}) as window:
            result = service.begin_edit_session("another-action")
        self.assertTrue(result["blocked"])
        window.assert_called_once_with("saved-session")

    def test_approval_launch_failure_has_a_reopenable_transaction(self):
        service = object.__new__(EditorService)
        with patch.object(service, "_launch_approval", side_effect=OSError("launcher unavailable")):
            result = service._try_launch_approval("transaction-1")
        self.assertFalse(result["approval_window_opened"])
        self.assertIn("reopen_approval", result["message"])

    def test_reopen_approval_does_not_create_or_execute_another_transaction(self):
        from unittest.mock import Mock
        from tests.helpers import transaction
        from gxp_editor.service import EditorServiceError
        service = object.__new__(EditorService)
        service.state = Mock()
        service.state.get_transaction.return_value = {"state": "awaiting_approval", "artifact_path": "unused.json", "session_id": "session-1"}
        service.state.get_session.return_value = {"conversation_id": "", "owner_revision": 0}
        with patch("gxp_editor.service.load_artifact", return_value=transaction()), patch.object(service, "_launch_approval") as launch:
            self.assertTrue(service.reopen_approval("transaction-1")["approval_window_opened"])
            launch.assert_called_once_with("transaction-1", owner_revision=0)
            service.state.create_transaction.assert_not_called()
            service.state.get_transaction.return_value["state"] = "executing"
            with self.assertRaises(EditorServiceError):
                service.reopen_approval("transaction-1")
    def test_begin_with_existing_session_offers_manual_abort_before_any_work(self):
        service = object.__new__(EditorService)
        from unittest.mock import Mock
        service.state = Mock()
        service.state.active_session.return_value = {"id": "old-session", "state": "awaiting_approval"}
        with patch.object(service, "_components") as components, patch.object(
            service, "abort_edit_session", return_value={"confirmation_window_opened": True, "aborted": False}
        ) as abort:
            result = service.begin_edit_session("new-action")
        components.assert_not_called()
        abort.assert_called_once_with("old-session")
        self.assertTrue(result["blocked"])
        self.assertFalse(result["aborted"])

    def test_abort_tool_opens_window_without_releasing_session(self):
        service = object.__new__(EditorService)
        from unittest.mock import Mock
        service.state = Mock()
        service.state.get_session.return_value = {"state": "awaiting_approval"}
        with patch("gxp_editor.service.subprocess.Popen") as launch:
            result = service.abort_edit_session("old-session")
        self.assertTrue(result["confirmation_window_opened"])
        self.assertFalse(result["aborted"])
        launch.assert_called_once()
        self.assertIn("abort_session_app.py", launch.call_args.args[0][1])
        service.state.update_session.assert_not_called()

    def test_draft_capability_does_not_claim_production_readiness(self):
        config = editor_config()
        service = object.__new__(EditorService)
        service.state = FakeState()
        components = (
            config,
            FakeDatabase(),
            FakeRepository(),
            FakeCompiler(),
            UnavailableRuntimeValidator(config.runtime_references),
        )
        with patch.object(service, "_components", return_value=components), patch(
            "gxp_editor.service.approval_secret", return_value="test-secret"
        ):
            status = service.editor_status()

        self.assertTrue(status["planning_ready"])
        self.assertTrue(status["write_ready"])
        self.assertFalse(status["production_ready"])
        self.assertTrue(status["draft_write_ready"])
        self.assertFalse(status["runtime_compatibility_verified"])
        self.assertFalse(status["gates"]["runtime_snapshot"]["ready"])
        self.assertFalse(status["gates"]["runtime_snapshot"]["blocking"])
        self.assertIn("must pass", status["gates"]["runtime_snapshot"]["warning"])

    def test_status_probes_runtime_by_default(self):
        config = editor_config()
        service = object.__new__(EditorService)
        service.state = FakeState()
        runtime = Mock()
        runtime.status.return_value = {"available": True, "production_compatible": True}
        with patch.object(service, "_components", return_value=(
            config, FakeDatabase(), FakeRepository(), FakeCompiler(), runtime,
        )), patch("gxp_editor.service.approval_secret", return_value="test-secret"):
            status = service.editor_status()
        runtime.status.assert_called_once_with()
        self.assertTrue(status["runtime_validation"]["available"])
        self.assertFalse(status["runtime_compatibility_verified"])

    def test_skipped_runtime_probe_still_checks_all_write_gates(self):
        config = editor_config()
        service = object.__new__(EditorService)
        service.state = FakeState()
        database = Mock(wraps=FakeDatabase())
        repository = Mock(wraps=FakeRepository())
        compiler = Mock(wraps=FakeCompiler())
        runtime = Mock()
        with patch.object(service, "_components", return_value=(
            config, database, repository, compiler, runtime,
        )), patch("gxp_editor.service.approval_secret", return_value="test-secret") as key:
            status = service.editor_status(probe_runtime=False)
        runtime.status.assert_not_called()
        database.status.assert_called_once_with()
        repository.index_status.assert_called_once_with()
        repository.grant_status.assert_called_once_with(
            account_mode=config.database.account_mode,
            expected_grants_sha256=config.database.expected_grants_sha256,
        )
        compiler.describe_registry.assert_called_once_with()
        compiler.compiler_binding.assert_called_once_with()
        key.assert_called_once_with(config)
        self.assertTrue(status["write_ready"])
        self.assertTrue(status["runtime_validation"]["probe_skipped"])
        self.assertFalse(status["runtime_validation"]["available"])
        self.assertFalse(status["runtime_compatibility_verified"])
        self.assertFalse(status["gates"]["runtime_snapshot"]["ready"])
        self.assertFalse(status["gates"]["runtime_snapshot"]["blocking"])

    def test_begin_reuses_its_runtime_probe_including_failed_probe(self):
        config = editor_config()
        draft = baseline()
        draft["data_json"] = json.loads(draft["data"])
        for available in (True, False):
            with self.subTest(available=available):
                service = object.__new__(EditorService)
                service.state = Mock()
                service.state.active_session.return_value = None
                service.state.target_session.return_value = None
                service.state.create_session.return_value = {
                    "id": "session-1", "ref_id": draft["ref_id"], "design_id": draft["id"],
                    "action_code": draft["code"], "action_name": draft["name"],
                }
                repository = FakeRepository()
                repository.load_draft_by_identifier = Mock(return_value=draft)
                compiler = FakeCompiler()
                compiler.validate_design = Mock(return_value={"validation": {"valid": True}})
                runtime = Mock()
                runtime.status.return_value = {"available": True}
                if not available:
                    runtime.status.side_effect = RuntimeError("runtime references unavailable")
                with patch.object(service, "_components", return_value=(
                    config, FakeDatabase(), repository, compiler, runtime,
                )), patch("gxp_editor.service.approval_secret", return_value="test-secret"), patch(
                    "gxp_editor.service.BackupStore",
                ) as backup_store:
                    backup_store.return_value.create.return_value = {"backup_id": "backup-1", "files": {}}
                    result = service.begin_edit_session("action")
                runtime.status.assert_called_once_with()
                self.assertTrue(result["write_ready"])
                self.assertEqual("runtime_snapshot" in result["failed_gates"], not available)

    def test_empty_csharp_never_claims_runtime_compatibility(self):
        from unittest.mock import Mock
        runtime=Mock();runtime.validate.return_value={"available":True,"success":True,
            "compilation_succeeded":False,"production_compatible":True,"source_mode":"deployment_snapshot"}
        evidence=EditorService._runtime_validation_evidence(runtime,"Action","")
        self.assertFalse(evidence["runtime_compatibility_verified"])
        self.assertIn("No candidate",evidence["warning"])

    def test_candidate_csharp_unavailable_or_failed_blocks_before_approval(self):
        from gxp_editor.service import EditorServiceError
        for available in (False, True):
            with self.assertRaisesRegex(EditorServiceError, "no approval"):
                EditorService._require_candidate_csharp_validation("class Candidate {}", {"available":available, "success":False})
        EditorService._require_candidate_csharp_validation("class Candidate {}", {"available":True, "success":True})
        EditorService._require_candidate_csharp_validation("", {"available":False, "success":False})

    def test_csharp_failure_creates_no_plan_file_or_state(self):
        from unittest.mock import Mock
        from gxp_editor.service import EditorServiceError
        draft=baseline();draft["data_json"]=json.loads(draft["data"])
        compiler=FakeCompiler();binding=compiler.compiler_binding()
        result={"design":draft["data_json"],"js_code":"","csharp_code":"invalid candidate C#","relation_data":{}}
        compiler.compile_patch=Mock(return_value=result)
        compiler.verify_compiled_result=Mock(return_value={"compiler_binding":binding,"validation":{"valid":True},
            "code_generation_complete":True,"generated_code_consistency_verified":True,"event_binding_validation":{}})
        session={"id":"session-1","state":"locked","design_id":draft["id"],
            "compiler_hash":binding["compiler_binding_sha256"],"baseline":draft}
        state=RecordingPlanState(session);service=object.__new__(EditorService);service.state=state
        with tempfile.TemporaryDirectory() as temporary, patch.object(service,"_components",return_value=(
            editor_config(),object(),PlanRepository(draft),compiler,object())), patch.object(
            service,"_runtime_validation_evidence",return_value={"available":True,"success":False,"failure_kind":"compilation_failed"}), patch(
            "gxp_editor.service.runtime_root",return_value=Path(temporary)):
            with self.assertRaisesRegex(EditorServiceError,"C# validation did not pass"):
                service.plan_canvas_patch("session-1",[{"operation":"replace_params"}], review_spec={
                    "schema_version": 1, "revision": "manual", "scope_policy": "manual_review", "requirements": []})
            self.assertFalse((Path(temporary)/"plans").exists())
        self.assertFalse(state.plan_created)
        self.assertFalse(state.transaction_created)

    def test_invalid_compiler_result_creates_no_plan_or_artifact(self):
        draft = baseline()
        draft["data_json"] = json.loads(draft["data"])
        compiler = BlockingPlanCompiler()
        session = {
            "id": "session-1",
            "state": "locked",
            "design_id": draft["id"],
            "compiler_hash": compiler.compiler_binding()["compiler_binding_sha256"],
            "baseline": draft,
        }
        state = RecordingPlanState(session)
        service = object.__new__(EditorService)
        service.state = state
        components = (
            editor_config(),
            object(),
            PlanRepository(draft),
            compiler,
            object(),
        )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            service, "_components", return_value=components
        ), patch("gxp_editor.service.runtime_root", return_value=Path(temporary)):
            with self.assertRaisesRegex(CompilerError, "no JavaScript"):
                service.plan_canvas_patch("session-1", [{"operation": "replace_params"}], review_spec={
                    "schema_version": 1, "revision": "manual", "scope_policy": "manual_review", "requirements": []})
            self.assertFalse((Path(temporary) / "plans").exists())

        self.assertFalse(state.plan_created)

    def test_prepare_write_revalidates_candidate_before_transaction_or_approval(self):
        draft = baseline()
        draft["data_json"] = json.loads(draft["data"])
        compiler = BlockingPrepareCompiler()
        binding = compiler.compiler_binding()
        desired = {
            "data": draft["data"],
            "js_code": "new-js",
            "csharp_code": "new-csharp",
            "relation_data": {},
        }
        hashes = EditorService._desired_hashes(desired)
        with tempfile.TemporaryDirectory() as temporary:
            artifact_path = Path(temporary) / "plan.json"
            artifact_path.write_text(
                json.dumps(
                    {
                        "base_content_hash": draft["content_hash"],
                        "compiler_hash": binding["compiler_binding_sha256"],
                        "compiler_binding": binding,
                        "desired": desired,
                        "validation": {"valid": True},
                        "event_binding_validation": {},
                        "code_generation_complete": True,
                        "generated_code_consistency_verified": True,
                        "hashes": hashes,
                        "business_review": build_business_review(
                            review_spec={"schema_version": 1, "revision": "manual", "scope_policy": "manual_review", "requirements": []},
                            baseline_design=draft, candidate_design=desired,
                            compiler_binding=binding,
                        ),
                    }
                ),
                encoding="utf-8",
            )
            plan = {
                "id": "plan-1",
                "session_id": "session-1",
                "status": "ready",
                "artifact_path": str(artifact_path),
                "base_hash": draft["content_hash"],
                "new_hash": plan_binding_hash(json.loads(artifact_path.read_text(encoding="utf-8"))),
                "compiler_hash": binding["compiler_binding_sha256"],
            }
            session = {
                "id": "session-1",
                "state": "planned",
                "design_id": draft["id"],
                "plan_id": "plan-1",
                "compiler_hash": binding["compiler_binding_sha256"],
                "baseline": draft,
            }
            state = RecordingPlanState(session, plan)
            service = object.__new__(EditorService)
            service.state = state
            components = (
                editor_config(),
                object(),
                PlanRepository(draft),
                compiler,
                object(),
            )
            with patch.object(service, "editor_status", return_value={"write_ready": True, "gates": {}}) as status_probe, patch.object(
                service, "_components", return_value=components
            ), patch.object(service, "_launch_approval") as launch:
                with self.assertRaisesRegex(CompilerError, "Candidate canvas is invalid"):
                    service.prepare_write("plan-1")
                compiler.verify_candidate_artifact = Mock(return_value={
                    "validation": {"valid": True}, "event_binding_validation": {},
                    "compiler_binding": binding, "code_generation_complete": True,
                    "generated_code_consistency_verified": True,
                })
                from gxp_editor.service import EditorServiceError
                with patch.object(service, "_runtime_validation_evidence", return_value={
                    "available": True, "success": False, "failure_kind": "compilation_failed",
                }) as candidate_validation:
                    with self.assertRaisesRegex(EditorServiceError, "C# validation did not pass"):
                        service.prepare_write("plan-1")
                candidate_validation.assert_called_once_with(components[-1], draft["code"], "new-csharp")
                self.assertEqual(status_probe.call_count, 2)
                status_probe.assert_called_with(probe_runtime=False)

        self.assertFalse(state.transaction_created)
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
