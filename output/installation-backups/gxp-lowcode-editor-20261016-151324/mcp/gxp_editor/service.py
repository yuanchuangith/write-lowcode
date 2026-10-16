from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from .approval import load_artifact, save_artifact
from .backup import BackupStore
from .compiler import CompilerBridge
from .config import ConfigurationError, approval_secret, default_config_path, load_config, runtime_root
from .db import EditorDatabase
from .hashing import relation_snapshot_hash, sha256_json, sha256_text
from .repository import EditorRepository, _flag
from .runtime_validation import RuntimeCsharpValidator
from .state import StateError, StateStore
from .transaction import build_transaction_artifact, validate_transaction_artifact
from .reference_repair import build_repair, inspection_projection, scan_references
from .recovery import recover_session


class EditorServiceError(RuntimeError):
    pass


class EditorService:
    def __init__(self, conversation_id: str = ""):
        if not isinstance(conversation_id, str) or len(conversation_id) > 128:
            raise EditorServiceError("对话标记无效。")
        self._conversation_id = conversation_id
        self.state = StateStore()

    @property
    def conversation_id(self) -> str:
        return getattr(self, "_conversation_id", "")

    def _owned_session(self, session_id: str, *, include_baseline: bool = True) -> dict[str, Any]:
        session = self.state.get_session(session_id) if include_baseline else self.state.get_session(session_id, include_baseline=False)
        if session.get("conversation_id", "") != self.conversation_id:
            raise EditorServiceError("此流程已归其他对话，当前对话不能操作。请使用会话标记接手。")
        return session

    @staticmethod
    def _session_summary(session: dict[str, Any] | None) -> dict[str, Any] | None:
        if session is None:
            return None
        return {key: session.get(key) for key in ("id", "state", "action_code", "action_name", "design_id", "ref_id", "transaction_id", "conversation_id", "owner_revision")}

    def create_conversation(self, label: str = "") -> dict[str, str]:
        return self.state.create_conversation(label)

    def attach_edit_session(self, session_id: str) -> dict[str, Any]:
        from contextlib import ExitStack
        from .execution_lock import approval_window_lock, execution_lock
        from .process_check import assert_approval_process_stopped
        if not self.conversation_id:
            raise EditorServiceError("接手前请创建当前对话的 conversation_id。")
        session = self.state.get_session(session_id, include_baseline=False)
        with ExitStack() as locks:
            if session.get("transaction_id"):
                transactions = self.state.session_transactions(session_id)
                transaction = next((row for row in transactions if row["id"] == session["transaction_id"]), None)
                lock_path = transaction["artifact_path"] if transaction else runtime_root() / "transactions" / f"{session['transaction_id']}.json"
                locks.enter_context(approval_window_lock(lock_path))
                locks.enter_context(execution_lock(lock_path))
                assert_approval_process_stopped(session["transaction_id"])
            current = self.state.get_session(session_id, include_baseline=False)
            if current.get("transaction_id") != session.get("transaction_id"):
                raise EditorServiceError("审批流程已变化，请重新接手。")
            attached = self.state.transfer_session(session_id, self.conversation_id,
                expected_owner=session.get("conversation_id", ""), expected_revision=session.get("owner_revision", 0),
                expected_transaction_id=session.get("transaction_id"), executor_idle=True)
        return {"attached": True, "session_id": session_id, "conversation_id": self.conversation_id,
                "owner_revision": attached["owner_revision"], "transaction_id": attached.get("transaction_id"),
                "session": self._session_summary(attached), "write_executed": False}

    def _components(self):
        config = load_config()
        database = EditorDatabase(config.database)
        return (
            config,
            database,
            EditorRepository(database),
            CompilerBridge(config),
            RuntimeCsharpValidator(config),
        )

    @staticmethod
    def _runtime_validation_evidence(
        runtime_validator: RuntimeCsharpValidator,
        action_code: str,
        csharp_code: str,
    ) -> dict[str, Any]:
        try:
            evidence = dict(runtime_validator.validate(action_code, csharp_code))
        except Exception as exc:
            evidence = {
                "available": False,
                "success": False,
                "production_compatible": False,
                "source_mode": runtime_validator.runtime.source_mode,
                "target_framework": runtime_validator.runtime.target_framework,
                "zero_backend_mode": True,
                "csharp_sha256": sha256_text(csharp_code),
                "diagnostics": [],
                "warning": f"Advisory local C# validation was unavailable: {exc}",
            }
        evidence["advisory_only"] = True
        evidence["runtime_compatibility_verified"] = bool(
            evidence.get("success") and evidence.get("production_compatible")
        )
        if not evidence["runtime_compatibility_verified"] and not evidence.get("warning"):
            evidence["warning"] = (
                "Local C# validation is advisory. Saving the unpublished database draft is allowed; "
                "manual publication is still required to generate the runtime DLL."
            )
        return evidence

    def editor_status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "configured": False,
            "planning_ready": False,
            "write_ready": False,
            "production_ready": False,
            "runtime_compatibility_verified": False,
            "conversation_id": self.conversation_id,
            "active_session": self._session_summary(self.state.active_session(self.conversation_id)),
            "gates": {},
        }
        try:
            config, database, repository, compiler, runtime_validator = self._components()
            result["configured"] = True
            result["config_path"] = str(default_config_path())
            result["frontend_repo"] = str(config.frontend_repo)
            result["backend_repo"] = str(config.backend_repo)
            result["source_repositories"] = {
                "frontend": {"path": str(config.frontend_repo), "role": "readonly_source_and_canvas_compiler"},
                "backend": {"path": str(config.backend_repo), "role": "readonly_source_evidence", "compiler_binding": False},
            }
            result["account_mode"] = config.database.account_mode
        except Exception as exc:
            result["gates"]["configuration"] = {"ready": False, "error": str(exc)}
            return result

        try:
            result["database"] = database.status()
            result["gates"]["database"] = {"ready": True}
        except Exception as exc:
            result["gates"]["database"] = {"ready": False, "error": str(exc)}
            return result
        try:
            approval_secret(config)
            result["gates"]["human_approval_key"] = {"ready": True}
        except Exception as exc:
            result["gates"]["human_approval_key"] = {"ready": False, "error": str(exc)}
        try:
            indexes = repository.index_status()
            index_ready = all(item["ready"] for item in indexes.values())
            result["indexes"] = indexes
            result["gates"]["indexes"] = {"ready": index_ready}
        except Exception as exc:
            result["gates"]["indexes"] = {"ready": False, "error": str(exc)}
        try:
            grants = repository.grant_status(
                account_mode=config.database.account_mode,
                expected_grants_sha256=config.database.expected_grants_sha256,
            )
            result["grants"] = grants
            result["gates"]["least_privilege"] = {
                "ready": bool(grants["safe"]),
                "account_mode": config.database.account_mode,
                "dedicated_least_privilege": bool(grants["dedicated_safe"]),
                "shared_account_warning": bool(grants["shared_account_warning"]),
                "privileged_shared_override": bool(grants["privileged_shared_override"]),
                "fingerprint_configured": bool(grants["fingerprint_configured"]),
                "fingerprint_matches": bool(grants["fingerprint_matches"]),
            }
        except Exception as exc:
            result["gates"]["least_privilege"] = {"ready": False, "error": str(exc)}
        try:
            registry = compiler.describe_registry()
            binding = compiler.compiler_binding()
            result["compiler"] = {
                **binding,
                "element_count": len(registry.get("elements") or []),
                "supported_operations": registry.get("supported_operations") or [],
            }
            result["gates"]["compiler"] = {
                "ready": True,
                "blocking": True,
                "source_provenance_verified": True,
                "canvas_validation_required": True,
                "code_generation_required": True,
            }
        except Exception as exc:
            result["gates"]["compiler"] = {"ready": False, "error": str(exc)}
        try:
            runtime_status = dict(runtime_validator.status())
            runtime_verified = bool(runtime_status.get("production_compatible"))
            runtime_status["advisory_only"] = True
            runtime_status["runtime_compatibility_verified"] = runtime_verified
            if not runtime_verified:
                runtime_status["warning"] = (
                    "Runtime references are not a deployment snapshot. This does not block saving the "
                    "unpublished database draft; manual publication must generate the runtime DLL."
                )
            result["runtime_validation"] = runtime_status
            result["runtime_compatibility_verified"] = runtime_verified
            result["gates"]["runtime_snapshot"] = {
                "ready": True,
                "blocking": False,
                "advisory": True,
                "runtime_compatibility_verified": runtime_verified,
                "warning": runtime_status.get("warning"),
            }
        except Exception as exc:
            warning = (
                f"Advisory runtime validation was unavailable: {exc}. This does not block saving the "
                "unpublished database draft; manual publication must generate the runtime DLL."
            )
            result["runtime_validation"] = {
                "available": False,
                "success": False,
                "production_compatible": False,
                "source_mode": runtime_validator.runtime.source_mode,
                "target_framework": runtime_validator.runtime.target_framework,
                "zero_backend_mode": True,
                "advisory_only": True,
                "runtime_compatibility_verified": False,
                "warning": warning,
            }
            result["gates"]["runtime_snapshot"] = {
                "ready": True,
                "blocking": False,
                "advisory": True,
                "runtime_compatibility_verified": False,
                "warning": warning,
            }
        result["planning_ready"] = all(
            bool(result["gates"].get(name, {}).get("ready"))
            for name in (
                "database",
                "human_approval_key",
                "compiler",
            )
        )
        result["write_ready"] = result["planning_ready"] and all(
            bool(result["gates"].get(name, {}).get("ready"))
            for name in ("indexes", "least_privilege")
        )
        result["production_ready"] = result["write_ready"]
        return result

    @staticmethod
    def _database_safety_binding(config, repository: EditorRepository) -> dict[str, Any]:
        grants = repository.grant_status(
            account_mode=config.database.account_mode,
            expected_grants_sha256=config.database.expected_grants_sha256,
        )
        if not grants["safe"]:
            raise EditorServiceError(
                "Database account safety gate is not ready: "
                + json.dumps(grants, ensure_ascii=False)
            )
        return {
            "account_mode": config.database.account_mode,
            "grants_sha256": grants["grants_sha256"],
            "shared_account": config.database.account_mode != "dedicated",
            "privileged_shared_override": config.database.account_mode == "shared_unrestricted",
        }

    @staticmethod
    def _element_keys(data_json: dict[str, Any]) -> set[str]:
        keys: set[str] = set()
        for action in data_json.get("actionData") or []:
            for node in action.get("data") or []:
                if node.get("elementKey"):
                    keys.add(str(node["elementKey"]))
        return keys

    @staticmethod
    def _compiler_design(draft: dict[str, Any]) -> dict[str, Any]:
        data = dict(draft["data_json"])
        data.update(
            {
                "refId": str(draft["ref_id"]),
                "id": str(draft["id"]),
                "name": str(draft.get("name") or ""),
                "code": str(draft.get("code") or ""),
                "describe": str(draft.get("describe") or ""),
                "isPublish": False,
                "version": int(draft.get("version") or 0),
            }
        )
        data.setdefault("actionData", [])
        data.setdefault("globalVariables", [])
        return data

    @staticmethod
    def _desired_hashes(desired: dict[str, Any]) -> dict[str, str]:
        return {
            "data_sha256": sha256_text(str(desired.get("data") or "")),
            "js_sha256": sha256_text(str(desired.get("js_code") or "")),
            "csharp_sha256": sha256_text(str(desired.get("csharp_code") or "")),
            "relation_sha256": sha256_json(desired.get("relation_data")),
        }

    @classmethod
    def _candidate_design(cls, baseline: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed = json.loads(str(desired.get("data") or ""))
        except json.JSONDecodeError as exc:
            raise EditorServiceError(f"Candidate ActionDesign data is invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise EditorServiceError("Candidate ActionDesign data must be a JSON object")
        candidate = dict(baseline)
        candidate["data_json"] = parsed
        return cls._compiler_design(candidate)

    @staticmethod
    def _compiler_binding_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
        return isinstance(expected, dict) and expected == actual

    def begin_edit_session(self, identifier: str) -> dict[str, Any]:
        return self._begin_session(identifier, repair=False)

    def begin_reference_repair_session(self, identifier: str) -> dict[str, Any]:
        return self._begin_session(identifier, repair=True)

    def _begin_session(self, identifier: str, *, repair: bool) -> dict[str, Any]:
        active = self.state.active_session(self.conversation_id)
        if active:
            result = self.abort_edit_session(active["id"])
            return {
                **result,
                "blocked": True,
                "active_session_id": active["id"],
                "active_session_state": active["state"],
                "message": "An edit session is active. Keep it or manually abort it in the local window. "
                           "Retry begin_edit_session only after it is finished or safely aborted.",
            }
        config, _, repository, compiler, runtime_validator = self._components()
        registry = compiler.describe_registry()
        draft = repository.load_draft_by_identifier(identifier)
        occupied = self.state.target_session(str(draft["id"]), str(draft["ref_id"]))
        if occupied:
            return {"blocked": True, "active_session_id": occupied["id"],
                    "message": "这份草稿已有独立编辑流程。请关闭原审批窗口后，用会话标记显式接手；其他草稿可并行编辑。"}
        supported = {str(item["key"]) for item in registry.get("elements") or [] if item.get("supported", True)}
        used = self._element_keys(draft["data_json"])
        unknown = sorted(used - supported)
        if unknown:
            raise EditorServiceError("Compiler coverage is incomplete for elementKey: " + ", ".join(unknown))
        compiler_design = self._compiler_design(draft)
        validation = {} if repair else compiler.validate_design(compiler_design)
        try:
            runtime_artifacts = runtime_validator.status()
        except Exception as exc:
            runtime_artifacts = {"available": False, "zero_backend_mode": True, "error": str(exc)}
        backup = BackupStore(integrity_key=approval_secret(config)).create(
            draft,
            {
                "bridge_sha256": registry["bridge_sha256"],
                "compiler_binding": compiler.compiler_binding(),
                "registry_hash": registry.get("registry_hash"),
                "rule_source_sha256": registry.get("rule_source_sha256"),
                "frontend_version": registry.get("frontend_version"),
                "elements": sorted(used),
                "validation": validation.get("validation"),
                "session_mode": "reference_repair" if repair else "edit",
            },
            runtime_artifacts,
        )
        report = None
        if repair:
            # Backup the original bytes before inspecting or projecting any binding.
            report = scan_references(compiler_design, registry)
            compiler.validate_design(inspection_projection(compiler_design, report))
        binding = compiler.compiler_binding()
        session = self.state.create_session(
            draft,
            backup["backup_id"],
            binding["compiler_binding_sha256"],
            session_mode="reference_repair" if repair else "edit",
            conversation_id=self.conversation_id,
        )
        status = self.editor_status()
        return {
            "session_id": session["id"],
            "session_marker": session["id"],
            "conversation_id": self.conversation_id,
            "owner_revision": session.get("owner_revision", 0),
            "ref_id": session["ref_id"],
            "design_id": session["design_id"],
            "action_code": session["action_code"],
            "action_name": session["action_name"],
            "backup_id": backup["backup_id"],
            "backup_manifest": backup["files"],
            "baseline_content_hash": draft["content_hash"],
            "compiler_hash": binding["compiler_binding_sha256"],
            "compiler_binding": binding,
            "element_keys": sorted(used),
            "validation": validation.get("validation"),
            "planning_ready": status["planning_ready"],
            "write_ready": status["write_ready"],
            "production_ready": status["production_ready"],
            "failed_gates": {name: gate for name, gate in status["gates"].items() if not gate.get("ready")},
            "session_mode": session.get("session_mode", "edit"),
            **({"reference_summary": report["summary"], "original_canvas_validated": False,
                "structure_validated": True, "candidate_validation_required": True} if report is not None else {}),
        }

    def inspect_component_contract(self, component_type: str) -> dict[str, Any]:
        """Read compiled event signatures; this does not verify a page instance."""
        if not isinstance(component_type, str) or not component_type.strip():
            raise EditorServiceError("component_type is required")
        _, _, _, compiler, _ = self._components()
        return compiler.describe_component_contract(component_type)

    def plan_canvas_patch(self, session_id: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
        session = self._owned_session(session_id)
        if session.get("session_mode", "edit") != "edit":
            raise EditorServiceError("Reference repair sessions require plan_reference_repairs")
        return self._plan_patch(session_id, operations)

    def inspect_reference_repairs(self, session_id: str, *, offset: int = 0, limit: int = 30) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 100:
            raise EditorServiceError("Use offset >= 0 and limit between 1 and 100")
        _, report, _ = self._reference_report(session_id)
        return {"session_id": session_id, "summary": report["summary"],
                "issues": report["issues"][offset:offset + limit], "offset": offset,
                "next_offset": offset + limit if offset + limit < len(report["issues"]) else None,
                "write_executed": False}

    def _reference_report(self, session_id: str):
        session = self._owned_session(session_id)
        if session.get("session_mode") != "reference_repair" or session["state"] not in {"locked", "planned"}:
            raise EditorServiceError("A locked or planned reference repair session is required")
        _, _, repository, compiler, _ = self._components()
        current = repository.load_draft_by_id(session["design_id"])
        if current["content_hash"] != session["baseline"]["content_hash"]:
            raise EditorServiceError("Draft changed after the repair session began")
        if compiler.compiler_binding()["compiler_binding_sha256"] != session["compiler_hash"]:
            raise EditorServiceError("Frontend compiler binding changed after the repair session began")
        design = self._compiler_design(session["baseline"])
        return design, scan_references(design, compiler.describe_registry()), compiler

    def plan_reference_repairs(self, session_id: str, choices: list[dict[str, str]] | None = None) -> dict[str, Any]:
        design, report, _ = self._reference_report(session_id)
        repaired = build_repair(design, report, [] if choices is None else choices)
        if repaired["blocked"]:
            return {"session_id": session_id, "blocked": True, "summary": report["summary"],
                    "unresolved": repaired["unresolved"], "plan_created": False, "write_executed": False}
        return self._plan_patch(session_id, repaired["operations"], repaired=repaired)

    def _plan_patch(self, session_id: str, operations: list[dict[str, Any]], *, repaired=None) -> dict[str, Any]:
        _, _, repository, compiler, runtime_validator = self._components()
        session = self._owned_session(session_id)
        if session["state"] not in {"locked", "planned"}:
            raise StateError(f"Session state {session['state']!r} cannot be planned")
        current = repository.load_draft_by_id(session["design_id"])
        if current["content_hash"] != session["baseline"]["content_hash"]:
            raise EditorServiceError("Draft changed after the edit session began")
        registry = compiler.describe_registry()
        binding = compiler.compiler_binding()
        if binding["compiler_binding_sha256"] != session["compiler_hash"]:
            raise EditorServiceError("Frontend compiler source or bridge changed after the edit session began")
        result = (compiler.compile_reference_repair(repaired) if repaired is not None else
                  compiler.compile_patch(self._compiler_design(session["baseline"]), operations))
        compiler_evidence = compiler.verify_compiled_result(result)
        if not self._compiler_binding_matches(binding, compiler_evidence["compiler_binding"]):
            raise EditorServiceError("Compiler binding changed while the canvas patch was being generated")
        design = result["design"]
        data_text = json.dumps(
            {
                "actionData": design.get("actionData") or [],
                "globalVariables": design.get("globalVariables") or [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        desired = {
            "data": data_text,
            "js_code": result["js_code"],
            "csharp_code": result["csharp_code"],
            "relation_data": result["relation_data"],
        }
        runtime_validation = self._runtime_validation_evidence(
            runtime_validator,
            str(current.get("code") or ""),
            desired["csharp_code"],
        )
        plan_artifact = {
            "session_id": session_id,
            "base_content_hash": current["content_hash"],
            "compiler_hash": binding["compiler_binding_sha256"],
            "compiler_binding": binding,
            "registry_hash": registry.get("registry_hash"),
            "rule_source_sha256": registry.get("rule_source_sha256"),
            "frontend_version": registry.get("frontend_version"),
            "operations": operations,
            "desired": desired,
            "validation": compiler_evidence["validation"],
            "event_binding_validation": compiler_evidence.get("event_binding_validation"),
            "code_generation_complete": compiler_evidence["code_generation_complete"],
            "runtime_validation": runtime_validation,
            "diff": result.get("diff") or [],
            "hashes": self._desired_hashes(desired),
            "reference_repairs": result.get("reference_repairs") or [],
        }
        new_hash = sha256_json(plan_artifact["hashes"])
        plan_dir = runtime_root() / "plans"
        plan_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = plan_dir / f"{uuid.uuid4().hex}.json"
        artifact_path.write_text(json.dumps(plan_artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        plan_id = self.state.create_plan(
            session_id,
            str(artifact_path),
            current["content_hash"],
            new_hash,
            binding["compiler_binding_sha256"],
            conversation_id=self.conversation_id,
            owner_revision=session.get("owner_revision", 0),
        )
        return {
            "plan_id": plan_id,
            "session_id": session_id,
            "operation_count": len(operations),
            "diff": plan_artifact["diff"],
            "validation": plan_artifact["validation"],
            "event_binding_validation": plan_artifact["event_binding_validation"],
            "runtime_validation": plan_artifact["runtime_validation"],
            "compiler_binding": binding,
            "code_generation_complete": True,
            "hashes": plan_artifact["hashes"],
            "write_executed": False,
            "reference_repairs": plan_artifact["reference_repairs"],
        }

    def _launch_approval(self, transaction_id: str, *, owner_revision: int | None = None) -> None:
        from .execution_lock import approval_window_lock
        transaction = self.state.get_transaction(transaction_id)
        session = self._owned_session(transaction["session_id"])
        revision = session.get("owner_revision", 0)
        if owner_revision is not None and owner_revision != revision:
            raise EditorServiceError("会话归属已变化，原请求不能打开审批。")
        with approval_window_lock(transaction["artifact_path"]):
            if transaction["state"] != "awaiting_approval":
                raise EditorServiceError("只有尚未执行的审批可以打开。")
        plugin_root = Path(__file__).resolve().parents[2]
        script = plugin_root / "scripts" / "approval_app.py"
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        executable = pythonw if pythonw.exists() else python
        subprocess.Popen([str(executable), str(script), transaction_id, self.conversation_id, str(revision)], cwd=plugin_root)

    def _try_launch_approval(self, transaction_id: str, *, owner_revision: int | None = None) -> dict[str, Any]:
        from .execution_lock import ApprovalWindowBusyError
        try:
            self._launch_approval(transaction_id, owner_revision=owner_revision)
            return {"approval_window_opened": True}
        except ApprovalWindowBusyError as exc:
            return {"approval_window_opened": True, "already_open": True, "message": str(exc)}
        except OSError as exc:
            return {"approval_window_opened": False, "approval_window_error": str(exc),
                    "message": "审批记录已创建，窗口未能启动；可调用 reopen_approval 重开或取消未执行审批。"}

    def prepare_write(self, plan_id: str) -> dict[str, Any]:
        plan = self.state.get_plan(plan_id)
        session = self._owned_session(plan["session_id"])
        status = self.editor_status()
        if not status["write_ready"]:
            raise EditorServiceError("Database write safety gates are not ready: " + json.dumps(status["gates"], ensure_ascii=False))
        config, _, repository, compiler, runtime_validator = self._components()
        database_safety = self._database_safety_binding(config, repository)
        if plan["status"] != "ready" or session["state"] != "planned" or session.get("plan_id") != plan_id:
            raise EditorServiceError("Only the current ready plan can enter approval")
        artifact = json.loads(Path(plan["artifact_path"]).read_text(encoding="utf-8"))
        current = repository.load_draft_by_id(session["design_id"])
        if (
            current["content_hash"] != artifact.get("base_content_hash")
            or plan.get("base_hash") != artifact.get("base_content_hash")
        ):
            raise EditorServiceError("Draft changed after the plan was generated")
        binding = compiler.compiler_binding()
        if (
            plan.get("compiler_hash") != artifact.get("compiler_hash")
            or session.get("compiler_hash") != artifact.get("compiler_hash")
            or binding["compiler_binding_sha256"] != artifact.get("compiler_hash")
            or not self._compiler_binding_matches(artifact.get("compiler_binding"), binding)
        ):
            raise EditorServiceError("Frontend compiler source, rules, registry, or bridge changed after planning")
        desired = artifact.get("desired")
        if not isinstance(desired, dict):
            raise EditorServiceError("Plan artifact is missing the desired ActionDesign")
        hashes = self._desired_hashes(desired)
        if artifact.get("hashes") != hashes or plan.get("new_hash") != sha256_json(hashes):
            raise EditorServiceError("Candidate design or generated-code hashes changed after planning")
        candidate_design = self._candidate_design(session["baseline"], desired)
        if session.get("session_mode") == "reference_repair":
            if scan_references(candidate_design, compiler.describe_registry())["summary"]["total"]:
                raise EditorServiceError("Candidate still contains invalid structured variable references")
            repair_report = scan_references(self._compiler_design(session["baseline"]), compiler.describe_registry())
            repairs = artifact.get("reference_repairs") or []
            rebuilt = build_repair(self._compiler_design(session["baseline"]), repair_report, [
                {"repair_id": item["repair_id"], "candidate_id": item["candidate_id"]}
                for item in repairs if item.get("selection") == "confirmed"
            ])
            if rebuilt["blocked"] or rebuilt["repairs"] != repairs:
                raise EditorServiceError("Reference repair provenance changed after planning")
            expected_nodes = {(g["key"], n["key"]): n for g in rebuilt["design"]["actionData"] for n in g["data"]}
            actual_nodes = {(g["key"], n["key"]): n for g in candidate_design["actionData"] for n in g["data"]}
            affected = {(item["group_key"], item["node_key"]) for item in repairs}
            for key in affected:
                node = expected_nodes[key]
                params = node["paramsValue"]
                output = (params.get("outputParams") or {}).get("variableName")
                if node["elementKey"] == "SetVariable" and isinstance(output, dict) and output.get("paramTypes") == "custom":
                    # Native generateActionData synchronizes this declared output type.
                    output["dataType"] = params["inputParams"]["variableType"]
            if set(expected_nodes) != set(actual_nodes) or any(
                expected_nodes[key]["paramsValue"] != actual_nodes[key]["paramsValue"]
                for key in expected_nodes
            ):
                raise EditorServiceError("Candidate parameters differ from the recorded reference repairs")
        compiler_evidence = compiler.verify_candidate_artifact(
            candidate_design, desired, baseline_design=self._compiler_design(session["baseline"]))
        if (
            compiler_evidence.get("validation") != artifact.get("validation")
            or compiler_evidence.get("event_binding_validation") != artifact.get("event_binding_validation")
            or not compiler_evidence.get("code_generation_complete")
            or not self._compiler_binding_matches(compiler_evidence.get("compiler_binding"), binding)
            or artifact.get("code_generation_complete") is not True
        ):
            raise EditorServiceError("Candidate canvas validation or generated-code evidence is no longer valid")
        runtime_validation = self._runtime_validation_evidence(
            runtime_validator,
            str(current.get("code") or ""),
            desired["csharp_code"],
        )
        transaction_id = uuid.uuid4().hex
        transaction_artifact = build_transaction_artifact(
            transaction_id=transaction_id,
            session_id=session["id"],
            plan_id=plan_id,
            kind="write",
            baseline=current,
            desired_design=desired,
            current_relations=repository.load_relations(session["design_id"]),
            desired_relations=desired["relation_data"],
            database_safety=database_safety,
            runtime_validation=runtime_validation,
            compiler_evidence={
                "compiler_binding": binding,
                "validation": compiler_evidence["validation"],
                "event_binding_validation": compiler_evidence.get("event_binding_validation"),
                "code_generation_complete": True,
                "hashes": hashes,
                "reference_repairs": artifact.get("reference_repairs") or [],
            },
        )
        tx_dir = runtime_root() / "transactions"
        tx_dir.mkdir(parents=True, exist_ok=True)
        tx_path = tx_dir / f"{transaction_id}.json"
        save_artifact(tx_path, transaction_artifact)
        self.state.create_transaction(session["id"], plan_id, "write", str(tx_path), transaction_id=transaction_id,
                                      conversation_id=self.conversation_id, owner_revision=session.get("owner_revision", 0))
        approval_window = self._try_launch_approval(transaction_id, owner_revision=session.get("owner_revision", 0))
        return {
            "transaction_id": transaction_id,
            "state": "awaiting_approval",
            "statement_count": len(transaction_artifact["statements"]),
            **approval_window,
            "write_executed": False,
            "runtime_validation": runtime_validation,
            "compiler_evidence": compiler_evidence,
            "manual_publication_required": True,
        }

    def get_write_status(self, transaction_id: str) -> dict[str, Any]:
        transaction = self.state.get_transaction(transaction_id)
        session = self._owned_session(transaction["session_id"], include_baseline=False)
        artifact_error = None
        try:
            artifact = load_artifact(transaction["artifact_path"])
            if not isinstance(artifact, dict):
                raise ValueError("审批文件不是有效对象")
        except (OSError, ValueError) as exc:
            artifact = {}
            artifact_error = str(exc)
        return {
            "transaction_id": transaction_id,
            "kind": transaction["kind"],
            "state": transaction["state"],
            "message": transaction["message"],
            "artifact_available": artifact_error is None,
            "artifact_error": artifact_error,
            "handling_available": True,
            "statement_count": len(artifact.get("statements") or []),
            "approved_count": len(artifact.get("approvals") or {}),
            "executed": bool(artifact.get("executed")) or transaction["state"] == "saved",
            "session_released": session["state"] not in {"locked", "planned", "awaiting_approval", "saved", "saved_state_pending", "verified", "restore_pending"},
            "conversation_id": self.conversation_id,
            "session_marker": transaction["session_id"],
            "retry_requires_new_plan": transaction["state"] in {"failed_no_write", "rolled_back", "closed_unverified"} or session["state"] == "closed_unverified",
            "old_content_hash": artifact.get("old_content_hash"),
            "new_content_hash": artifact.get("new_content_hash"),
        }

    def reopen_approval(self, transaction_id: str, *, owner_revision: int | None = None) -> dict[str, Any]:
        transaction = self.state.get_transaction(transaction_id)
        session = self._owned_session(transaction["session_id"])
        if owner_revision is not None and session.get("owner_revision", 0) != owner_revision:
            raise EditorServiceError("会话归属已变化，原窗口不能重新打开审批。")
        artifact = load_artifact(transaction["artifact_path"])
        validate_transaction_artifact(artifact)
        if transaction["state"] != "awaiting_approval" or artifact.get("execution_started"):
            raise EditorServiceError("只有尚未执行的审批可以重新打开。")
        window = self._try_launch_approval(transaction_id, owner_revision=session.get("owner_revision", 0))
        return {"transaction_id": transaction_id, **window, "write_executed": False}

    def recover_edit_session(self, session_id: str) -> dict[str, Any]:
        session = self._owned_session(session_id)
        return recover_session(session_id, state=self.state, conversation_id=self.conversation_id,
                               owner_revision=session.get("owner_revision", 0))

    def prepare_restore(self, session_id: str, backup_id: str) -> dict[str, Any]:
        session = self._owned_session(session_id)
        status = self.editor_status()
        if not status["write_ready"]:
            raise EditorServiceError("Database restore safety gates are not ready")
        config, _, repository, _, runtime_validator = self._components()
        database_safety = self._database_safety_binding(config, repository)
        if session["state"] not in {"saved", "saved_state_pending", "verified"}:
            raise EditorServiceError(f"Session state {session['state']!r} cannot start a restore")
        if backup_id != session["backup_id"]:
            raise EditorServiceError("Restore is limited to the immutable backup created by this session")
        backup = BackupStore(integrity_key=approval_secret(config)).load(session["ref_id"], backup_id)
        current = repository.load_draft_by_id(session["design_id"])
        if _flag(current.get("is_publish")) or _flag(current.get("isDeleted")):
            raise EditorServiceError("The target is no longer an active unpublished draft")
        if str(backup["manifest"].get("design_id") or "") != session["design_id"]:
            raise EditorServiceError("Backup design identity does not match the active session")
        original = backup["design.json"]
        runtime_validation = self._runtime_validation_evidence(
            runtime_validator,
            str(original.get("code") or current.get("code") or ""),
            str(original.get("csharp_code") or ""),
        )
        transaction_id = uuid.uuid4().hex
        transaction_artifact = build_transaction_artifact(
            transaction_id=transaction_id,
            session_id=session_id,
            plan_id=None,
            kind="restore",
            baseline=current,
            desired_design={
                "data": original["data"],
                "js_code": original.get("js_code") or "",
                "csharp_code": original.get("csharp_code") or "",
                "last_modification_time": original["last_modification_time"],
            },
            current_relations=repository.load_relations(session["design_id"]),
            desired_relations=backup["relations.json"],
            database_safety=database_safety,
            runtime_validation=runtime_validation,
        )
        tx_dir = runtime_root() / "transactions"
        tx_dir.mkdir(parents=True, exist_ok=True)
        tx_path = tx_dir / f"{transaction_id}.json"
        save_artifact(tx_path, transaction_artifact)
        self.state.create_transaction(session_id, None, "restore", str(tx_path), transaction_id=transaction_id,
                                      conversation_id=self.conversation_id, owner_revision=session.get("owner_revision", 0))
        approval_window = self._try_launch_approval(transaction_id, owner_revision=session.get("owner_revision", 0))
        return {
            "transaction_id": transaction_id,
            "state": "awaiting_approval",
            "statement_count": len(transaction_artifact["statements"]),
            **approval_window,
            "runtime_validation": runtime_validation,
            "manual_publication_required": True,
        }

    def verify_saved_draft(self, session_id: str) -> dict[str, Any]:
        session = self._owned_session(session_id)
        _, _, repository, _, _ = self._components()
        if not session.get("transaction_id"):
            raise EditorServiceError("Session has no write or restore transaction")
        transaction = self.state.get_transaction(session["transaction_id"])
        if transaction["state"] in {"executing", "failed", "saved_state_pending"}:
            result = self.recover_edit_session(session_id)
            if result["outcome"] != "saved":
                return result
            transaction = self.state.get_transaction(transaction["id"])
        if transaction["state"] != "saved":
            raise EditorServiceError(f"Transaction is not fully saved: {transaction['state']}")
        artifact = load_artifact(transaction["artifact_path"])
        current = repository.load_draft_by_id(session["design_id"])
        if current["content_hash"] != artifact["new_content_hash"]:
            raise EditorServiceError("Saved draft hash does not match the approved transaction")
        relations = repository.load_relations(session["design_id"])
        relation_hash = relation_snapshot_hash(relations)
        if relation_hash != artifact["expected_relation_hash"]:
            raise EditorServiceError("Saved relation snapshot does not match the approved transaction")
        self.state.update_session(session_id, "verified", conversation_id=self.conversation_id,
                                  owner_revision=session.get("owner_revision", 0), expected_states=("saved", "verified"),
                                  expected_transaction_id=transaction["id"])
        return {
            "verified": True,
            "design_id": session["design_id"],
            "ref_id": session["ref_id"],
            "content_hash": current["content_hash"],
            "relation_hash": relation_hash,
            "transaction_kind": transaction["kind"],
            "runtime_finalized": False,
            "runtime_mode": "existing_backend_lazy_compile_after_manual_publish",
            "manual_publication_required": True,
        }

    def finish_edit_session(self, session_id: str) -> dict[str, Any]:
        session = self._owned_session(session_id)
        if session["state"] != "verified":
            raise EditorServiceError("Session must be verified before it can be finished")
        self.state.update_session(session_id, "finished", conversation_id=self.conversation_id,
                                  owner_revision=session.get("owner_revision", 0), expected_states=("verified",),
                                  expected_transaction_id=session.get("transaction_id"))
        return {
            "finished": True,
            "action_code": session["action_code"],
            "action_name": session["action_name"],
            "ref_id": session["ref_id"],
            "message": (
                "Draft data and relations were verified. No backend endpoint, Redis key, DLL, or PDB was changed by "
                "the editor. Inspect the canvas in ActionDesign and publish manually if accepted; the existing "
                "publish path clears its cache and the existing runtime lazily compiles C# on first use."
            ),
        }

    def abort_edit_session(self, session_id: str) -> dict[str, Any]:
        session = self._owned_session(session_id, include_baseline=False)
        if session["state"] not in {"locked", "planned", "awaiting_approval", "restore_pending", "saved", "saved_state_pending", "verified"}:
            raise EditorServiceError("会话已经结束，无需释放占用。")
        plugin_root = Path(__file__).resolve().parents[2]
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        subprocess.Popen(
            [str(pythonw if pythonw.exists() else python), str(plugin_root / "scripts" / "abort_session_app.py"), session_id, self.conversation_id, str(session.get("owner_revision", 0))],
            cwd=plugin_root,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {"aborted": False, "session_id": session_id, "confirmation_window_opened": True,
                "message": "The user must choose whether to keep or safely abort the old session. Recheck session state before retrying."}
