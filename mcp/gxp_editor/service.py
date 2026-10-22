from __future__ import annotations

import json
import subprocess
import sys
import uuid
from functools import wraps
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .approval import load_artifact, save_artifact
from .action_context import action_context, publication_target, queue_publication_targets
from .backup import BackupStore
from .compiler import CompilerBridge
from .config import ConfigurationError, approval_secret, default_config_path, load_config, runtime_root
from .db import EditorDatabase
from .hashing import relation_snapshot_hash, sha256_json, sha256_text
from .repository import EditorRepository, _flag
from .runtime_validation import RuntimeCsharpValidator
from .state import StateError, StateStore, saved_verification
from .review_response import evidence_summary, review_page, review_response
from .transaction import build_transaction_artifact, validate_transaction_artifact
from .reference_repair import build_repair, inspection_projection, scan_references
from .recovery import recover_session
from .logic_review import (
    build_business_review, normalize_review_spec, plan_binding_hash,
    require_write_business_review, validate_business_review,
)
from .logic_scenarios import LogicScenarioRunner


_COMPONENT_REQUEST = ContextVar("gxp_editor_component_request", default=None)


def component_request(method):
    """Reuse components only within one tool request, including nested helpers."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        current = _COMPONENT_REQUEST.get()
        if current is not None and current["owner"] is self:
            return method(self, *args, **kwargs)
        token = _COMPONENT_REQUEST.set({"owner": self})
        try:
            return method(self, *args, **kwargs)
        finally:
            _COMPONENT_REQUEST.reset(token)
    return wrapped


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

    def create_edit_queue(self) -> dict[str, Any]:
        return {"queue_id": self.state.create_edit_queue(self.conversation_id),
                "conversation_id": self.conversation_id, "write_executed": False}

    def get_edit_queue(self, queue_id: str) -> dict[str, Any]:
        items = self.state.edit_queue(queue_id, self.conversation_id)
        return {"queue_id": queue_id, "items": [
            {**self._session_summary(item), "position": item["position"], "plan_id": item.get("plan_id"),
             "plan_status": item.get("plan_status"), "transaction_state": item.get("transaction_state"),
             "owned": item["conversation_id"] == self.conversation_id and item["owner_revision"] == item["queued_owner_revision"],
             "action_context": item["action_context"]} for item in items],
                "publication_targets": queue_publication_targets(items, self.conversation_id), "write_executed": False}

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
        request = _COMPONENT_REQUEST.get()
        if request is not None and request["owner"] is self and "components" in request:
            return request["components"]
        config = load_config()
        database = EditorDatabase(config.database)
        components = (
            config,
            database,
            EditorRepository(database),
            CompilerBridge(config),
            RuntimeCsharpValidator(config),
        )
        if request is not None and request["owner"] is self:
            request["components"] = components
        return components

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
                "available": getattr(exc, "code", "") == "compilation_failed",
                "success": False,
                "compilation_succeeded": False,
                "production_compatible": False,
                "source_mode": runtime_validator.runtime.source_mode,
                "target_framework": runtime_validator.runtime.target_framework,
                "zero_backend_mode": True,
                "csharp_sha256": sha256_text(csharp_code),
                "diagnostics": list(getattr(exc, "diagnostics", [])),
                "failure_kind": getattr(exc, "code", "checker_unavailable"),
                "warning": f"Local C# validation failed: {exc}",
            }
        evidence["advisory_only"] = False
        evidence["runtime_compatibility_verified"] = bool(
            evidence.get("available") and evidence.get("success") and evidence.get("compilation_succeeded") is True
            and evidence.get("production_compatible") and not evidence.get("reference_conflicts")
            and evidence.get("source_mode") == "deployment_snapshot"
        )
        if not evidence["runtime_compatibility_verified"] and not evidence.get("warning"):
            evidence["warning"] = (
                "Local C# compiled with assembly reference conflicts; production compatibility is unverified."
                if evidence.get("reference_conflicts") else
                "No candidate C# compilation was required; production behavior remains unverified."
                if not csharp_code.strip() else
                "Local C# compilation passed against development references. Production compatibility "
                "still requires matching deployment references and runtime acceptance."
            )
        return evidence

    @staticmethod
    def _require_candidate_csharp_validation(csharp_code: str, evidence: dict[str, Any]) -> None:
        if csharp_code.strip() and not (evidence.get("available") is True and evidence.get("success") is True):
            reason = evidence.get("warning") or evidence.get("failure_kind") or "C# compilation did not succeed"
            raise EditorServiceError("Candidate C# validation did not pass; no approval can be created. " + str(reason))

    @component_request
    def editor_status(
        self, *, runtime_artifacts: dict[str, Any] | None = None, probe_runtime: bool = True,
    ) -> dict[str, Any]:
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
            if runtime_artifacts is not None:
                runtime_status = dict(runtime_artifacts)
            elif probe_runtime:
                runtime_status = dict(runtime_validator.status())
            else:
                runtime_status = {
                    "available": False,
                    "production_compatible": False,
                    "probe_skipped": True,
                    "zero_backend_mode": True,
                }
            runtime_verified = False  # A status probe verifies no candidate or deployed action.
            runtime_status["reference_snapshot_available"] = bool(runtime_status.get("available"))
            runtime_status["advisory_only"] = True
            runtime_status["runtime_compatibility_verified"] = runtime_verified
            if not runtime_verified:
                runtime_status["warning"] = (
                    "SDK/reference availability does not validate a candidate. Nonblank candidate C# must "
                    "compile before approval; production compatibility requires deployment evidence."
                )
            result["runtime_validation"] = runtime_status
            result["runtime_compatibility_verified"] = runtime_verified
            result["gates"]["runtime_snapshot"] = {
                "ready": bool(runtime_status.get("available")),
                "blocking": False,
                "candidate_compilation_required": True,
                "advisory": True,
                "probe_skipped": bool(runtime_status.get("probe_skipped")),
                "runtime_compatibility_verified": runtime_verified,
                "warning": runtime_status.get("warning"),
            }
        except Exception as exc:
            warning = (
                f"Local C# checker is unavailable: {exc}. Nonblank candidate C# must pass "
                "compilation before a plan or approval can be created."
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
                "ready": False,
                "blocking": False,
                "candidate_compilation_required": True,
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
        result["draft_write_ready"] = result["write_ready"]
        result["production_ready"] = False
        result["production_readiness_reason"] = (
            "Production readiness requires matching deployment references, manual publication "
            "and runtime acceptance evidence; draft write readiness does not establish it."
        )
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

    def begin_edit_session(self, identifier: str, *, queue_id: str = "") -> dict[str, Any]:
        return self._begin_session(identifier, repair=False, queue_id=queue_id)

    def begin_reference_repair_session(self, identifier: str, *, queue_id: str = "") -> dict[str, Any]:
        return self._begin_session(identifier, repair=True, queue_id=queue_id)

    @component_request
    def _begin_session(self, identifier: str, *, repair: bool, queue_id: str = "") -> dict[str, Any]:
        if queue_id:
            self.state.edit_queue(queue_id, self.conversation_id)
        active = self.state.active_session(self.conversation_id)
        if active and not (queue_id and active.get("queue_id") == queue_id):
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
            runtime_artifacts = (runtime_validator.status() if str(draft.get("csharp_code") or "").strip()
                                 else runtime_validator.no_source_evidence())
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
            queue_id=queue_id,
        )
        status = self.editor_status(runtime_artifacts=runtime_artifacts)
        return {
            "session_id": session["id"],
            "queue_id": queue_id or None,
            "session_marker": session["id"],
            "conversation_id": self.conversation_id,
            "owner_revision": session.get("owner_revision", 0),
            "ref_id": session["ref_id"],
            "design_id": session["design_id"],
            "action_code": session["action_code"],
            "action_name": session["action_name"],
            "action_context": action_context(draft),
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
            "supported_operations": registry.get("supported_operations") or [],
            "source_repositories": status.get("source_repositories") or {},
            "readiness_snapshot": {key: status[key] for key in
                ("planning_ready", "write_ready", "production_ready", "gates") if key in status},
            **({"reference_summary": report["summary"], "original_canvas_validated": False,
                "structure_validated": True, "candidate_validation_required": True} if report is not None else {}),
        }

    def inspect_component_contract(self, component_type: str) -> dict[str, Any]:
        """Read compiled event signatures; this does not verify a page instance."""
        if not isinstance(component_type, str) or not component_type.strip():
            raise EditorServiceError("component_type is required")
        _, _, _, compiler, _ = self._components()
        return compiler.describe_component_contract(component_type)

    @component_request
    def inspect_edit_context(self, session_id: str, targets: list[dict[str, str]]) -> dict[str, Any]:
        from .edit_context import build_edit_context
        session = self._owned_session(session_id)
        if session["state"] not in {"locked", "planned", "awaiting_approval"}:
            raise EditorServiceError("Target inspection requires an active edit session")
        _, _, repository, compiler, _ = self._components()
        current = repository.load_draft_by_id(session["design_id"])
        if current["content_hash"] != session["baseline"]["content_hash"]:
            raise EditorServiceError("Draft changed after the edit session began; target evidence is stale")
        registry = compiler.describe_registry()
        binding = compiler.compiler_binding()
        if binding["compiler_binding_sha256"] != session["compiler_hash"]:
            raise EditorServiceError("Frontend compiler binding changed; target contracts are stale")
        result = build_edit_context(self._compiler_design(session["baseline"]), registry, targets)
        # Ownership may change while native source checks are running.
        latest = self._owned_session(session_id, include_baseline=False)
        if latest.get("owner_revision", 0) != session.get("owner_revision", 0):
            raise EditorServiceError("Session ownership changed while reading the edit context")
        response = {**result, "session_id": session_id, "backup_id": session["backup_id"],
                "design_id": session["design_id"], "baseline_content_hash": session["baseline"]["content_hash"],
                "compiler_binding": binding, "write_executed": False}
        if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > 1024 * 1024:
            raise EditorServiceError("Edit context exceeds 1 MiB; request fewer target nodes")
        return response

    def read_plan_review(self, plan_id: str, *, section="summary", offset=0, limit=10, item_id=""):
        plan = self.state.get_plan(plan_id)
        session = self._owned_session(plan["session_id"])
        artifact = load_artifact(plan["artifact_path"])
        self._validate_plan_review(plan, session, artifact)
        report = artifact["business_review"]
        page = review_page(report, section, offset, limit, item_id)
        latest = self._owned_session(session["id"], include_baseline=False)
        if latest.get("owner_revision", 0) != session.get("owner_revision", 0):
            raise EditorServiceError("Session ownership changed while reading the plan report")
        return {"plan_id": plan_id, "report_sha256": report["review_sha256"], "read_only": True,
                "current_plan": latest.get("plan_id") == plan_id,
                **page, "write_executed": False}

    def plan_canvas_patch(
        self, session_id: str, operations: list[dict[str, Any]], review_spec: dict[str, Any] | None = None,
        *, response_detail: str = "full",
    ) -> dict[str, Any]:
        session = self._owned_session(session_id)
        if session.get("session_mode", "edit") != "edit":
            raise EditorServiceError("Reference repair sessions require plan_reference_repairs")
        return self._plan_patch(session_id, operations, review_spec=review_spec, response_detail=response_detail)

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

    @component_request
    def plan_reference_repairs(
        self, session_id: str, choices: list[dict[str, str]] | None = None,
        review_spec: dict[str, Any] | None = None, *, response_detail: str = "full",
    ) -> dict[str, Any]:
        design, report, _ = self._reference_report(session_id)
        repaired = build_repair(design, report, [] if choices is None else choices)
        if repaired["blocked"]:
            return {"session_id": session_id, "blocked": True, "summary": report["summary"],
                    "unresolved": repaired["unresolved"], "plan_created": False, "write_executed": False}
        return self._plan_patch(session_id, repaired["operations"], repaired=repaired, review_spec=review_spec,
                                response_detail=response_detail)

    def _resolve_review_spec(self, session: dict[str, Any], supplied: dict[str, Any] | None) -> dict[str, Any]:
        previous = None
        if session.get("plan_id"):
            old_plan = self.state.get_plan(session["plan_id"])
            old_artifact = load_artifact(old_plan["artifact_path"])
            if old_artifact.get("business_review") is not None:
                self._validate_plan_review(old_plan, session, old_artifact)
                previous = old_artifact["business_review"]["spec"]
            elif old_plan.get("new_hash") != sha256_json(old_artifact.get("hashes")):
                raise EditorServiceError("已有计划的业务验收证据被移除或损坏，不能清空预期后重试。")
        if supplied is None and previous is not None:
            if "scope_policy" not in previous:
                raise EditorServiceError("旧计划未声明修改范围策略，请提交新的验收规则后重新规划。")
            return previous
        spec = normalize_review_spec(supplied)
        if previous is not None and sha256_json(spec) != sha256_json(previous):
            if spec.get("revision") == previous.get("revision"):
                raise EditorServiceError("修改验收规则或场景时必须提供新的 revision。")
            old_rules = {item["id"]: {field: item[field] for field in ("text", "targets")} for item in previous.get("requirements", [])}
            new_rules = {item["id"]: {field: item[field] for field in ("text", "targets")} for item in spec.get("requirements", [])}
            old_cases = {item["id"]: item for item in previous["scenarios"]["cases"]}
            new_cases = {item["id"]: item for item in spec["scenarios"]["cases"]}
            changed_existing_cases = any(
                case_id not in new_cases or any(
                    sha256_json(old_case.get(field)) != sha256_json(new_cases[case_id].get(field))
                    for field in ("environment", "steps", "expect", "requirement_ids")
                ) for case_id, old_case in old_cases.items()
            )
            old_assertions = {item["id"]: item for item in previous.get("canvas_assertions", [])}
            new_assertions = {item["id"]: item for item in spec.get("canvas_assertions", [])}
            changed_existing_assertions = any(
                assertion_id not in new_assertions or sha256_json(assertion) != sha256_json(new_assertions[assertion_id])
                for assertion_id, assertion in old_assertions.items()
            )
            changed_scope_policy = spec.get("scope_policy") != previous.get("scope_policy", "manual_review")
            if old_rules != new_rules or changed_existing_cases or changed_existing_assertions or changed_scope_policy:
                old_sources = {str(item.get("source_text") or "").strip() for item in previous.get("requirements", [])}
                new_sources = {str(item.get("source_text") or "").strip() for item in spec.get("requirements", [])}
                change_source = str(spec.get("change_source_text") or "").strip()
                previous_change_source = str(previous.get("change_source_text") or "").strip()
                if not (change_source and change_source != previous_change_source) and not (new_sources - old_sources - {""}):
                    raise EditorServiceError("修改或移除业务预期、已有场景或画布断言时必须附新的用户依据 source_text 或 change_source_text。")
        return spec

    @staticmethod
    def _validate_plan_review(plan: dict[str, Any], session: dict[str, Any], artifact: dict[str, Any]) -> None:
        if not isinstance(artifact.get("business_review"), dict):
            raise EditorServiceError("此计划缺少业务验收证据，请重新生成计划。")
        if plan.get("new_hash") != plan_binding_hash(artifact):
            raise EditorServiceError("计划或业务验收报告已变化，请重新生成计划。")
        if artifact.get("base_content_hash") != session["baseline"]["content_hash"] or plan.get("base_hash") != artifact.get("base_content_hash"):
            raise EditorServiceError("业务验收的修改前版本与会话备份不一致。")
        if "action_context" in artifact and artifact["action_context"] != action_context(session["baseline"]):
            raise EditorServiceError("计划的表单或动作归属与会话备份不一致，请重新生成计划。")
        validate_business_review(
            artifact["business_review"], baseline_design=session["baseline"],
            candidate_design=artifact.get("desired"), compiler_binding=artifact.get("compiler_binding"),
        )

    @component_request
    def _plan_patch(
        self, session_id: str, operations: list[dict[str, Any]], *, repaired=None,
        review_spec: dict[str, Any] | None = None, response_detail: str = "full",
    ) -> dict[str, Any]:
        if response_detail not in {"summary", "full"}:
            raise EditorServiceError("response_detail must be summary or full")
        config, _, repository, compiler, runtime_validator = self._components()
        session = self._owned_session(session_id)
        if session["state"] not in {"locked", "planned"}:
            raise StateError(f"Session state {session['state']!r} cannot be planned")
        current = repository.load_draft_by_id(session["design_id"])
        if current["content_hash"] != session["baseline"]["content_hash"]:
            raise EditorServiceError("Draft changed after the edit session began")
        spec = self._resolve_review_spec(session, review_spec)
        if repaired is None and type(operations) is list and operations and CompilerBridge.guarded_no_changes(
                self._compiler_design(session["baseline"]), operations):
            return self._no_changes_response(session)
        registry = compiler.describe_registry()
        binding = compiler.compiler_binding()
        if binding["compiler_binding_sha256"] != session["compiler_hash"]:
            raise EditorServiceError("Frontend compiler source or bridge changed after the edit session began")
        result = (compiler.compile_reference_repair(repaired) if repaired is not None else
                  compiler.compile_patch(self._compiler_design(session["baseline"]), operations))
        if result.get("no_change") is True:
            return self._no_changes_response(session)
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
        self._require_candidate_csharp_validation(desired["csharp_code"], runtime_validation)
        scenario_report = LogicScenarioRunner(config).run_pair(
            str(session["baseline"].get("js_code") or ""), desired["js_code"], spec["scenarios"],
        )
        business_review = build_business_review(
            review_spec=spec, baseline_design=session["baseline"], candidate_design=desired,
            compiler_binding=binding, scenario_report=scenario_report,
        )
        plan_artifact = {
            "session_id": session_id,
            "action_context": action_context(session["baseline"]),
            "base_content_hash": current["content_hash"],
            "compiler_hash": binding["compiler_binding_sha256"],
            "compiler_binding": binding,
            "registry_hash": registry.get("registry_hash"),
            "rule_source_sha256": registry.get("rule_source_sha256"),
            "frontend_version": registry.get("frontend_version"),
            "operations": operations,
            "desired": desired,
            "validation": compiler_evidence["validation"],
            **({"interface_metadata": compiler_evidence["interface_metadata"]} if compiler_evidence.get("interface_metadata") else {}),
            "event_binding_validation": compiler_evidence.get("event_binding_validation"),
            "code_generation_complete": compiler_evidence["code_generation_complete"],
            "generated_code_consistency_verified": compiler_evidence["generated_code_consistency_verified"],
            "runtime_validation": runtime_validation,
            "diff": result.get("diff") or [],
            "hashes": self._desired_hashes(desired),
            "reference_repairs": result.get("reference_repairs") or [],
            "business_review": business_review,
        }
        new_hash = plan_binding_hash(plan_artifact)
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
            initial_status="blocked" if business_review["status"] == "failed" else "ready",
            expected_plan_id=session.get("plan_id"),
        )
        return {
            "plan_id": plan_id,
            "session_id": session_id,
            "operation_count": len(operations),
            "action_context": plan_artifact["action_context"],
            **({"diff": plan_artifact["diff"]} if response_detail == "full" else {"diff_count": len(plan_artifact["diff"])}),
            "validation": plan_artifact["validation"] if response_detail == "full" else evidence_summary(plan_artifact["validation"]),
            "event_binding_validation": plan_artifact["event_binding_validation"] if response_detail == "full" else evidence_summary(plan_artifact["event_binding_validation"]),
            "runtime_validation": plan_artifact["runtime_validation"] if response_detail == "full" else evidence_summary(plan_artifact["runtime_validation"]),
            "compiler_binding": binding,
            "code_generation_complete": True,
            **({"interface_metadata": plan_artifact["interface_metadata"]} if plan_artifact.get("interface_metadata") else {}),
            "generated_code_consistency_verified": plan_artifact["generated_code_consistency_verified"],
            "hashes": plan_artifact["hashes"],
            "write_executed": False,
            **({"reference_repairs": plan_artifact["reference_repairs"]} if response_detail == "full" else
               {"reference_repair_count": len(plan_artifact["reference_repairs"])}),
            **review_response(business_review, plan_id, response_detail),
            "ready_for_approval": business_review["status"] != "failed",
            "blocked_reasons": ["业务验收未通过，请按 review_issues 查看失败的配置、范围或场景证据。"] if business_review["status"] == "failed" else [],
        }

    def _no_changes_response(self, session):
        existing = self.state.get_plan(session["plan_id"]) if session.get("plan_id") else None
        return {"session_id": session["id"], "no_changes": True, "plan_created": False,
                "ready_for_approval": False, "write_executed": False,
                "existing_plan_id": existing["id"] if existing else None,
                "existing_plan_status": existing["status"] if existing else None,
                "message": "目标身份和原值已核对，全部参数与原值相同，本次无需新计划或审批。" +
                           ("先前计划仍保留，本次调用未替换或取消它。" if existing else "")}

    def open_plan_review(self, plan_id: str, *, response_detail: str = "full") -> dict[str, Any]:
        """Open an immutable plan report without creating or approving a transaction."""
        if response_detail not in {"summary", "full"}:
            raise EditorServiceError("response_detail must be summary or full")
        plan = self.state.get_plan(plan_id)
        session = self._owned_session(plan["session_id"])
        artifact = load_artifact(plan["artifact_path"])
        self._validate_plan_review(plan, session, artifact)
        report = artifact["business_review"]
        review_dir = runtime_root() / "plan-reviews"
        review_dir.mkdir(parents=True, exist_ok=True)
        review_path = review_dir / f"{plan_id}-{uuid.uuid4().hex}.json"
        save_artifact(review_path, {
            "plan_id": plan_id, "business_review": report,
            "action_context": action_context(session["baseline"]),
            "before": session["baseline"].get("data_json") or json.loads(session["baseline"]["data"]),
            "after": json.loads(artifact["desired"]["data"]), "validation": artifact.get("validation"),
        })
        plugin_root = Path(__file__).resolve().parents[2]
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        try:
            subprocess.Popen([
                str(pythonw if pythonw.exists() else python), str(plugin_root / "scripts" / "approval_app.py"),
                "--plan-review", str(review_path),
            ], cwd=plugin_root)
            window = {"review_window_opened": True}
        except OSError as exc:
            window = {"review_window_opened": False, "review_window_error": str(exc)}
        return {
            "plan_id": plan_id, **review_response(report, plan_id, response_detail),
            **({key: artifact.get(key) for key in ("diff", "reference_repairs", "validation",
                "event_binding_validation", "runtime_validation", "compiler_binding")}
               if response_detail == "full" else {}),
            "ready_for_approval": plan["status"] == "ready" and session.get("plan_id") == plan_id,
            "action_context": action_context(session["baseline"]),
            "read_only": True, "write_executed": False, **window,
        }

    def _launch_approval(self, transaction_id: str, *, owner_revision: int | None = None) -> None:
        from .execution_lock import approval_queue_lock, approval_window_lock
        transaction = self.state.get_transaction(transaction_id)
        session = self._owned_session(transaction["session_id"])
        revision = session.get("owner_revision", 0)
        if owner_revision is not None and owner_revision != revision:
            raise EditorServiceError("会话归属已变化，原请求不能打开审批。")
        queue_id = session.get("queue_id") or ""
        if queue_id:
            # A transferred session must not rejoin its former queue implicitly.
            try:
                items = self.state.edit_queue(queue_id, self.conversation_id)
            except StateError:
                items = []
            if not any(item["id"] == session["id"] and item["queued_owner_revision"] == revision for item in items):
                queue_id = ""
        if queue_id:
            with approval_queue_lock(queue_id):
                pass
        with approval_window_lock(transaction["artifact_path"]):
            if transaction["state"] != "awaiting_approval":
                raise EditorServiceError("只有尚未执行的审批可以打开。")
        plugin_root = Path(__file__).resolve().parents[2]
        script = plugin_root / "scripts" / "approval_app.py"
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        executable = pythonw if pythonw.exists() else python
        arguments = ["--queue", queue_id, self.conversation_id] if queue_id else [transaction_id, self.conversation_id, str(revision)]
        subprocess.Popen([str(executable), str(script), *arguments], cwd=plugin_root)

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

    @component_request
    def prepare_write(self, plan_id: str, *, response_detail: str = "full") -> dict[str, Any]:
        if response_detail not in {"summary", "full"}:
            raise EditorServiceError("response_detail must be summary or full")
        plan = self.state.get_plan(plan_id)
        session = self._owned_session(plan["session_id"])
        artifact = load_artifact(plan["artifact_path"])
        self._validate_plan_review(plan, session, artifact)
        if plan["status"] == "blocked" or artifact["business_review"]["status"] == "failed":
            return {
                "plan_id": plan_id, **review_response(artifact["business_review"], plan_id, response_detail),
                "ready_for_approval": False, "approval_window_opened": False, "write_executed": False,
                "blocked_reasons": ["业务验收未通过，请修正失败的配置、范围或场景后重新规划。"],
            }
        if "scope_policy" not in artifact["business_review"]["spec"]:
            raise EditorServiceError("旧计划缺少修改范围策略，只可查看；请重新生成计划后审批。")
        status = self.editor_status(probe_runtime=False)
        if not status["write_ready"]:
            raise EditorServiceError("Database write safety gates are not ready: " + json.dumps(status["gates"], ensure_ascii=False))
        config, _, repository, compiler, runtime_validator = self._components()
        database_safety = self._database_safety_binding(config, repository)
        if plan["status"] != "ready" or session["state"] != "planned" or session.get("plan_id") != plan_id:
            raise EditorServiceError("Only the current ready plan can enter approval")
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
        if artifact.get("hashes") != hashes or plan.get("new_hash") != plan_binding_hash(artifact):
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
            or compiler_evidence.get("interface_metadata") != artifact.get("interface_metadata")
            or compiler_evidence.get("generated_code_consistency_verified") is not True
            or artifact.get("generated_code_consistency_verified") is not True
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
        self._require_candidate_csharp_validation(desired["csharp_code"], runtime_validation)
        transaction_id = uuid.uuid4().hex
        transaction_artifact = build_transaction_artifact(
            transaction_id=transaction_id,
            session_id=session["id"],
            plan_id=plan_id,
            kind="write",
            baseline={**current, "metadata": session["baseline"].get("metadata") or {}},
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
                "generated_code_consistency_verified": compiler_evidence["generated_code_consistency_verified"],
                "hashes": hashes,
                **({"interface_metadata": compiler_evidence["interface_metadata"]} if compiler_evidence.get("interface_metadata") else {}),
                "reference_repairs": artifact.get("reference_repairs") or [],
            },
            business_review=artifact["business_review"],
        )
        require_write_business_review(transaction_artifact)
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
            "queue_id": session.get("queue_id") or None,
            "statement_count": len(transaction_artifact["statements"]),
            **approval_window,
            "write_executed": False,
            "runtime_validation": runtime_validation if response_detail == "full" else evidence_summary(runtime_validation),
            "compiler_evidence": compiler_evidence if response_detail == "full" else {
                "code_generation_complete": compiler_evidence["code_generation_complete"],
                "generated_code_consistency_verified": compiler_evidence["generated_code_consistency_verified"],
                "compiler_binding_sha256": binding["compiler_binding_sha256"],
                "validation": evidence_summary(compiler_evidence["validation"]),
                "event_binding_validation": evidence_summary(compiler_evidence.get("event_binding_validation")),
            },
            "manual_publication_required": True,
            **review_response(artifact["business_review"], plan_id, response_detail),
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
        publication_targets = [publication_target(artifact.get("action_context"), session)] if (
            session.get("transaction_id") == transaction_id and transaction["state"] == "saved"
            and session["state"] == "finished") else []
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
            "verified": (session.get("transaction_id") == transaction_id
                         and transaction["state"] == "saved"
                         and session["state"] in {"verified", "finished"}),
            "session_state": session["state"],
            "saved_verification": saved_verification(session, transaction),
            "session_released": session["state"] not in {"locked", "planned", "awaiting_approval", "saved", "saved_state_pending", "verified", "restore_pending"},
            "conversation_id": self.conversation_id,
            "session_marker": transaction["session_id"],
            "retry_requires_new_plan": transaction["state"] in {"failed_no_write", "rolled_back", "closed_unverified"} or session["state"] == "closed_unverified",
            "old_content_hash": artifact.get("old_content_hash"),
            "new_content_hash": artifact.get("new_content_hash"),
            "publication_targets": publication_targets,
        }

    def reopen_approval(self, transaction_id: str, *, owner_revision: int | None = None) -> dict[str, Any]:
        transaction = self.state.get_transaction(transaction_id)
        session = self._owned_session(transaction["session_id"])
        if owner_revision is not None and session.get("owner_revision", 0) != owner_revision:
            raise EditorServiceError("会话归属已变化，原窗口不能重新打开审批。")
        artifact = load_artifact(transaction["artifact_path"])
        validate_transaction_artifact(artifact)
        require_write_business_review(artifact)
        if transaction["state"] != "awaiting_approval" or artifact.get("execution_started"):
            raise EditorServiceError("只有尚未执行的审批可以重新打开。")
        window = self._try_launch_approval(transaction_id, owner_revision=session.get("owner_revision", 0))
        return {"transaction_id": transaction_id, **window, "write_executed": False}

    def recover_edit_session(self, session_id: str) -> dict[str, Any]:
        session = self._owned_session(session_id)
        return recover_session(session_id, state=self.state, conversation_id=self.conversation_id,
                               owner_revision=session.get("owner_revision", 0))

    @component_request
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
            baseline={**current, "metadata": session["baseline"].get("metadata") or {}},
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
        from .execution_lock import execution_lock

        session = self._owned_session(session_id)
        if not session.get("transaction_id"):
            raise EditorServiceError("Session has no write or restore transaction")
        transaction = self.state.get_transaction(session["transaction_id"])
        if transaction["state"] in {"executing", "failed", "saved_state_pending"}:
            result = self.recover_edit_session(session_id)
            if result["outcome"] != "saved":
                return result
        # Do not race the approval worker's automatic post-commit completion.
        with execution_lock(transaction["artifact_path"]):
            return self._verify_saved_draft(session_id)

    def _verify_saved_draft(self, session_id: str) -> dict[str, Any]:
        session = self._owned_session(session_id)
        transaction = self.state.get_transaction(session["transaction_id"])
        receipt = saved_verification(session, transaction)
        if receipt:
            return {"verified": True, "design_id": session["design_id"], "ref_id": session["ref_id"],
                    "content_hash": receipt["content_hash"], "relation_hash": receipt["relation_hash"],
                    "saved_verification": receipt, "current_draft_rechecked": False,
                    "session_released": True, "transaction_kind": transaction["kind"],
                    "runtime_finalized": False, "manual_publication_required": True,
                    "publication_targets": [action_context(session["baseline"])]}
        if session["state"] == "finished" and transaction["state"] == "saved":
            return {"verified": True, "session_released": True, "design_id": session["design_id"],
                    "ref_id": session["ref_id"], "verification_scope": "previously_finished_session",
                    "current_draft_rechecked": False, "runtime_finalized": False,
                    "manual_publication_required": True,
                    "publication_targets": [action_context(session["baseline"])]}
        _, _, repository, _, _ = self._components()
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
        if session["state"] not in {"verified", "finished"}:
            raise EditorServiceError("Session must be verified before it can be finished")
        if session["state"] == "verified":
            self.state.update_session(session_id, "finished", conversation_id=self.conversation_id,
                                      owner_revision=session.get("owner_revision", 0), expected_states=("verified",),
                                      expected_transaction_id=session.get("transaction_id"))
        return {
            "finished": True,
            "publication_targets": [action_context(session["baseline"])],
            "action_code": session["action_code"],
            "action_name": session["action_name"],
            "ref_id": session["ref_id"],
            "message": (
                "Draft data and relations were verified. No backend endpoint, Redis key, DLL, or PDB was changed by "
                "the editor. Inspect the canvas in ActionDesign and publish manually if accepted; the existing "
                "publish path clears its cache and the existing runtime lazily compiles C# on first use."
            ),
        }

    def inspect_session_cancellation(self, session_id: str) -> dict[str, Any]:
        snapshot = self.state.cancellation_snapshot(session_id, conversation_id=self.conversation_id)
        session = snapshot["session"]
        try:
            self.state._require_chat_cancellable(snapshot)
            reason = None
        except StateError as exc:
            reason = str(exc)
        return {"session_id": session_id, "action_name": session["action_name"], "action_code": session["action_code"],
                "session_state": session["state"], "cancellable": reason is None, "reason": reason,
                "plans": [{key: row[key] for key in ("id", "status")} for row in snapshot["plans"]
                          if row["status"] in {"ready", "blocked", "frozen"}],
                "approvals": [{key: row[key] for key in ("id", "state")} for row in snapshot["transactions"]
                              if row["state"] == "awaiting_approval"],
                "cancellation_fingerprint": snapshot["fingerprint"] if reason is None else None,
                "requires_user_confirmation": reason is None, "write_executed": False,
                "message": "取消会使以上未执行计划和审批失效并释放本地会话；保留当前业务数据、备份和历史记录。"}

    def cancel_unexecuted_session(self, session_id: str, cancellation_fingerprint: str, user_confirmation: str) -> dict[str, Any]:
        from contextlib import ExitStack
        from .execution_lock import execution_lock
        if (not isinstance(user_confirmation, str) or not user_confirmation.strip() or len(user_confirmation) > 2000
                or not isinstance(cancellation_fingerprint, str) or len(cancellation_fingerprint) != 64):
            raise EditorServiceError("先展示具体取消范围，再取得用户明确同意；需要确认原文和对应的取消指纹。")
        snapshot = self.state.cancellation_snapshot(session_id, conversation_id=self.conversation_id)
        if snapshot["fingerprint"] != cancellation_fingerprint:
            raise EditorServiceError("会话、计划或审批已变化，请重新展示取消范围并取得用户确认。")
        self.state._require_chat_cancellable(snapshot)
        with ExitStack() as locks:
            for transaction in snapshot["transactions"]:
                if transaction["state"] == "awaiting_approval":
                    locks.enter_context(execution_lock(transaction["artifact_path"]))
            self.state.abort_session(session_id, conversation_id=self.conversation_id,
                                     owner_revision=snapshot["session"]["owner_revision"],
                                     expected_fingerprint=cancellation_fingerprint, user_confirmation=user_confirmation)
        return {"session_id": session_id, "cancelled": True, "session_released": True,
                "write_executed": False, "backups_retained": True, "audit_retained": True,
                "message": "已按用户确认取消未执行计划和审批并释放本地会话；业务数据和备份保留。旧审批不能再执行。"}

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
