from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import baseline, editor_config
from gxp_editor.compiler import CompilerError
from gxp_editor.hashing import sha256_json
from gxp_editor.service import EditorService


class FakeState:
    def active_session(self):
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
    def compile_patch(self, design, operations):
        return {"design": design}

    def verify_compiled_result(self, result):
        raise CompilerError("Compiler generated no JavaScript")


class BlockingPrepareCompiler(FakeCompiler):
    def verify_candidate_artifact(self, design, desired):
        raise CompilerError("Candidate canvas is invalid")


class ServiceStatusTests(unittest.TestCase):
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

    def test_runtime_validation_failure_is_advisory_for_database_writes(self):
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
        self.assertTrue(status["production_ready"])
        self.assertFalse(status["runtime_compatibility_verified"])
        self.assertTrue(status["gates"]["runtime_snapshot"]["ready"])
        self.assertFalse(status["gates"]["runtime_snapshot"]["blocking"])
        self.assertIn("does not block", status["gates"]["runtime_snapshot"]["warning"])

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
                service.plan_canvas_patch("session-1", [{"operation": "replace_params"}])
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
                        "code_generation_complete": True,
                        "hashes": hashes,
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
                "new_hash": sha256_json(hashes),
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
            with patch.object(service, "editor_status", return_value={"write_ready": True, "gates": {}}), patch.object(
                service, "_components", return_value=components
            ), patch.object(service, "_launch_approval") as launch:
                with self.assertRaisesRegex(CompilerError, "Candidate canvas is invalid"):
                    service.prepare_write("plan-1")

        self.assertFalse(state.transaction_created)
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
