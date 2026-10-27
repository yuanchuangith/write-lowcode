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
from .compiler import CompilerBridge, CompilerError
from .telemetry import stage, add_external, timed
from .config import ConfigurationError, approval_secret, default_config_path, load_config, runtime_root
from .db import EditorDatabase
from .hashing import relation_snapshot_hash, sha256_json, sha256_text
from .handoff_store import environment_scope
from .repository import EditorRepository, _flag
from .runtime_validation import RuntimeCsharpValidator
from .state import ACTIVE_STATES, StateError, StateStore, saved_verification, assert_session_database_scope
from .review_response import evidence_summary, review_page, review_response
from .post_write import look_graph_review
from .workflow_guidance import write_status_guidance
from .transaction import build_transaction_artifact, validate_transaction_artifact
from .reference_repair import build_repair, inspection_projection, scan_references
from .recovery import archive_edit_session, recover_session
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

    def _owned_session(self, session_id: str, *, include_baseline: bool = True,
                       check_database: bool = True) -> dict[str, Any]:
        session = self.state.get_session(session_id) if include_baseline else self.state.get_session(session_id, include_baseline=False)
        if session.get("conversation_id", "") != self.conversation_id:
            raise EditorServiceError("此流程已归其他对话，当前对话不能操作。请使用会话标记接手。")
        if check_database:
            config = self._components()[0].database if session.get("database_scope") else None
            assert_session_database_scope(session, config)
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
        graph_reviews = []
        for item in items:
            if (item["conversation_id"] == self.conversation_id
                    and item["owner_revision"] == item["queued_owner_revision"] and item.get("transaction_id")):
                review = self._graph_review(item, {"id": item["transaction_id"],
                    "session_id": item["id"], "state": item.get("transaction_state")})
                if review:
                    graph_reviews.append(review)
        return {"queue_id": queue_id, "items": [
            {**self._session_summary(item), "position": item["position"], "plan_id": item.get("plan_id"),
             "plan_status": item.get("plan_status"), "transaction_state": item.get("transaction_state"),
             "owned": item["conversation_id"] == self.conversation_id and item["owner_revision"] == item["queued_owner_revision"],
             "action_context": item["action_context"]} for item in items],
                "publication_targets": queue_publication_targets(items, self.conversation_id),
                "look_graph_reviews": graph_reviews,
                "pending_graph_review_count": sum(item["review_required"] for item in graph_reviews),
                "write_executed": False}

    def attach_edit_session(self, session_id: str) -> dict[str, Any]:
        from contextlib import ExitStack
        from .execution_lock import approval_window_lock, execution_lock
        from .process_check import assert_approval_process_stopped
        if not self.conversation_id:
            raise EditorServiceError("接手前请创建当前对话的 conversation_id。")
        session = self.state.get_session(session_id, include_baseline=False)
        assert_session_database_scope(session)
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
            with stage("runtime.csharp_validation"):
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
        finally:
            add_external(getattr(runtime_validator, "last_performance", None))
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
        return self._readiness_status(runtime_artifacts=runtime_artifacts, probe_runtime=probe_runtime)

    @component_request
    @timed("readiness.status")
    def _readiness_status(
        self, *, runtime_artifacts: dict[str, Any] | None = None, probe_runtime: bool = True,
        defer_compiler: bool = False,
    ) -> dict[str, Any]:
        """Prepare may defer its compiler scan until after draft/relation IO.

        This helper never grants write readiness without that scan. The public
        status endpoint always verifies all gates.
        """
        result: dict[str, Any] = {
            "configured": False,
            "planning_ready": False,
            "noncompiler_write_ready": False,
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
            if defer_compiler:
                registry, binding = None, None
            else:
                registry, binding = compiler.registry_and_binding()
            if defer_compiler:
                result["gates"]["compiler"] = {"ready": False, "deferred": True, "blocking": True}
            else:
                self._set_compiler_status(result, registry, binding)
        except Exception as exc:
            result["gates"]["compiler"] = {"ready": False, "error": str(exc)}
        result["noncompiler_write_ready"] = all(bool(result["gates"].get(name, {}).get("ready"))
            for name in ("database", "human_approval_key", "indexes", "least_privilege"))
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
    def _set_compiler_status(result, registry, binding):
        result["compiler"] = {**binding, "element_count": len(registry.get("elements") or []),
                              "supported_operations": registry.get("supported_operations") or []}
        result["gates"]["compiler"] = {"ready": True, "blocking": True,
            "source_provenance_verified": True, "canvas_validation_required": True, "code_generation_required": True}

    @staticmethod
    def _database_safety_binding(config, repository: EditorRepository, *, verified_grants=None) -> dict[str, Any]:
        # Only pass the immediately preceding readiness result within this request.
        grants = verified_grants if verified_grants is not None else repository.grant_status(
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

    @staticmethod
    def _baseline_codegen_artifacts(draft: dict[str, Any]) -> dict[str, Any]:
        """Read only saved artifacts from this session's immutable snapshot."""
        relations = {}
        for row in draft.get("relations") or []:
            if row.get("isDeleted", 0) != 0:
                continue
            key = str(row.get("action_data_id") or "")
            if not key or key in relations:
                raise EditorServiceError("Saved relation data has a missing or duplicate node identity")
            relations[key] = row.get("data")
        return {"js_code": str(draft.get("js_code") or ""),
                "csharp_code": str(draft.get("csharp_code") or ""), "relation_data": relations}

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

    def begin_edit_session(self, identifier: str, *, queue_id: str = "", handoff_ref: dict | None = None,
                           response_detail: str = "full") -> dict[str, Any]:
        return self._begin_response(self._begin_session(identifier, repair=False, queue_id=queue_id,
            handoff_ref=handoff_ref, response_detail=response_detail), response_detail)

    def begin_reference_repair_session(self, identifier: str, *, queue_id: str = "", response_detail: str = "full") -> dict[str, Any]:
        return self._begin_response(self._begin_session(identifier, repair=True, queue_id=queue_id,
            response_detail=response_detail), response_detail)

    @staticmethod
    def _begin_response(result: dict[str, Any], detail: str) -> dict[str, Any]:
        if detail == "full" or result.get("blocked"):
            return result
        result = dict(result)
        for field in ("validation", "handoff"):
            if isinstance(result.get(field), dict):
                result[field] = evidence_summary(result[field])
        diagnostics = result.get("canvas_diagnostics", [])
        if diagnostics:
            result["canvas_diagnostic_count"] = len(diagnostics)
            result["canvas_diagnostics"] = [evidence_summary(row) for row in diagnostics[:3]]
        result["response_detail"] = "summary"
        return result

    @component_request
    def _begin_session(self, identifier: str, *, repair: bool, queue_id: str = "", handoff_ref: dict | None = None,
                       response_detail: str = "full") -> dict[str, Any]:
        if response_detail not in {"summary", "full"}:
            raise EditorServiceError("response_detail must be summary or full")
        if queue_id:
            self.state.edit_queue(queue_id, self.conversation_id)
        active = self.state.active_session(self.conversation_id)
        same_target_retry = bool(active and active.get("state") in {"locked", "planned", "awaiting_approval"}
                                 and identifier in {active.get("ref_id"), active.get("design_id"), active.get("action_code")})
        if active and not same_target_retry and not (queue_id and active.get("queue_id") == queue_id):
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
        if active:
            assert_session_database_scope(active, config.database)
        with stage("database.load_draft"):
            draft = repository.load_draft_by_identifier(identifier)
        handoff = None
        edit_context = None
        evidence_candidates = None
        if handoff_ref is not None:
            from .handoff_import import inspect_handoff, unavailable_handoff
            from .handoff_store import HandoffError
            try:
                handoff = inspect_handoff(handoff_ref, draft, config.database)
            except (HandoffError, OSError, ValueError, TypeError, KeyError) as exc:
                return unavailable_handoff(exc)
            if handoff["status"] not in {"matched", "diagnosis_only"}:
                return {"blocked": True, "session_created": False, "plan_created": False,
                        "write_executed": False, "handoff_status": handoff["status"], "handoff": handoff,
                        "next_step": handoff["next_step"]}
        elif not repair:
            from .handoff_import import discover_handoffs
            evidence_candidates = discover_handoffs(draft, config.database)
        occupied = self.state.target_session(str(draft["id"]), str(draft["ref_id"]))
        if occupied:
            if occupied.get("conversation_id") == self.conversation_id:
                return self._reuse_edit_session(occupied["id"], draft, compiler, repair=repair,
                                                queue_id=queue_id, handoff=handoff,
                                                evidence_candidates=evidence_candidates, response_detail=response_detail)
            return {"blocked": True, "active_session_id": occupied["id"],
                    "message": "这份草稿已有独立编辑流程。请关闭原审批窗口后，用会话标记显式接手；其他草稿可并行编辑。"}
        registry = compiler.describe_registry()
        supported = {str(item["key"]) for item in registry.get("elements") or [] if item.get("supported", True)}
        used = self._element_keys(draft["data_json"])
        unknown = sorted(used - supported)
        if unknown:
            raise EditorServiceError("Compiler coverage is incomplete for elementKey: " + ", ".join(unknown))
        compiler_design = self._compiler_design(draft)
        if handoff is not None and handoff["status"] == "matched":
            from .edit_context import build_edit_context
            # Build bounded target evidence before acquiring local occupancy.
            try:
                edit_context = build_edit_context(compiler_design, registry, handoff["targets"], response_detail=response_detail)
            except (ValueError, TypeError, KeyError) as exc:
                from .handoff_import import unavailable_handoff
                return unavailable_handoff(exc)
        validation = {} if repair else compiler.validate_design(compiler_design)
        try:
            runtime_artifacts = (runtime_validator.status() if str(draft.get("csharp_code") or "").strip()
                                 else runtime_validator.no_source_evidence())
        except Exception as exc:
            runtime_artifacts = {"available": False, "zero_backend_mode": True, "error": str(exc)}
        with stage("backup.create"):
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
        # Finish fallible readiness reads before acquiring local draft occupancy.
        status = self.editor_status(runtime_artifacts=runtime_artifacts)
        if not status.get("gates", {}).get("compiler", {}).get("ready"):
            raise EditorServiceError("Compiler readiness changed before session creation: "
                                     + json.dumps(status.get("gates", {}).get("compiler"), ensure_ascii=False))
        # editor_status just checked the source after backup/projection and database
        # work. Its binding is safe for adjacent consumers until the next IO.
        binding = {key: value for key, value in status["compiler"].items()
                   if key not in {"element_count", "supported_operations"}}
        session = self.state.create_session(
            draft,
            backup["backup_id"],
            binding["compiler_binding_sha256"],
            session_mode="reference_repair" if repair else "edit",
            conversation_id=self.conversation_id,
            queue_id=queue_id,
            database_scope=environment_scope(config.database),
        )
        if edit_context is not None and response_detail == "summary":
            edit_context = self._sign_context_guards(edit_context, session, config)
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
            "canvas_diagnostics": validation.get("canvas_diagnostics", []),
            "write_ready": status["write_ready"],
            "production_ready": status["production_ready"],
            "failed_gates": {name: gate for name, gate in status["gates"].items() if not gate.get("ready")},
            "session_mode": session.get("session_mode", "edit"),
            **({"handoff": handoff} if handoff is not None else {}),
            **({"edit_context": {**edit_context,
                "session_id": session["id"], "backup_id": backup["backup_id"],
                "baseline_content_hash": draft["content_hash"], "compiler_binding": binding,
                "write_executed": False}} if edit_context is not None else {}),
            **({"evidence_candidates": evidence_candidates} if evidence_candidates is not None else {}),
            "supported_operations": registry.get("supported_operations") or [],
            "source_repositories": status.get("source_repositories") or {},
            "readiness_snapshot": {key: status[key] for key in
                ("planning_ready", "write_ready", "production_ready", "gates") if key in status},
            **({"reference_summary": report["summary"], "original_canvas_validated": False,
                "structure_validated": True, "candidate_validation_required": True} if report is not None else {}),
        }

    def _reuse_edit_session(self, session_id, draft, compiler, *, repair, queue_id, handoff,
                            evidence_candidates=None, response_detail="full"):
        """A retry resumes the owned flow; it never grants or transfers approval."""
        session = self._owned_session(session_id)
        failure = {"blocked": True, "session_reused": False, "session_created": False,
                   "active_session_id": session_id, "write_executed": False,
                   "handling_options": self._handling_options(session)}
        if (session["state"] not in {"locked", "planned", "awaiting_approval"}
                or session.get("queue_id", "") != queue_id
                or session.get("session_mode", "edit") != ("reference_repair" if repair else "edit")
                or str(draft["id"]) != session["design_id"] or str(draft["ref_id"]) != session["ref_id"]):
            return {**failure, "reason_code": "session_retry_mismatch",
                    "message": "现有流程的目标、模式、队列或状态与本次请求不同，请检查现有会话后继续。"}
        _, _, repository, _, _ = self._components()
        if (draft["content_hash"] != session["baseline"]["content_hash"]
                or relation_snapshot_hash(repository.load_relations(session["design_id"])) !=
                relation_snapshot_hash(session["baseline"].get("relations") or [])):
            return {**failure, "reason_code": "session_baseline_changed",
                    "message": "草稿或关系已变化，现有流程需要核对结果；未创建新会话或继承审批。"}
        if handoff is not None and handoff["status"] == "matched":
            registry, binding = compiler.registry_and_binding()
        else:
            binding = compiler.compiler_binding()
        if binding["compiler_binding_sha256"] != session["compiler_hash"]:
            return {**failure, "reason_code": "session_compiler_changed",
                    "message": "现有流程的编译器绑定已变化，请先处理旧流程，再建立新基线。"}
        next_tool = ("get_write_status" if session["state"] == "awaiting_approval" else
                     "plan_reference_repairs" if repair else "plan_canvas_patch")
        result = {"session_id": session_id, "session_marker": session_id, "session_reused": True,
                  "session_created": False, "conversation_id": self.conversation_id,
                  "owner_revision": session.get("owner_revision", 0), "state": session["state"],
                  "queue_id": queue_id or None, "session_mode": session.get("session_mode", "edit"),
                  "ref_id": session["ref_id"], "design_id": session["design_id"],
                  "action_code": session["action_code"], "action_name": session["action_name"],
                  "backup_id": session["backup_id"], "baseline_content_hash": draft["content_hash"],
                  "compiler_binding": binding, "compiler_hash": session["compiler_hash"],
                  "plan_id": session.get("plan_id"), "transaction_id": session.get("transaction_id"),
                  "next_tool": next_tool, "write_executed": False, "approval_granted": False}
        if evidence_candidates is not None:
            result["evidence_candidates"] = evidence_candidates
        if handoff is not None:
            result["handoff"] = handoff
        if handoff is not None and handoff["status"] == "matched":
            from .edit_context import build_edit_context
            context = build_edit_context(self._compiler_design(session["baseline"]), registry, handoff["targets"],
                                         response_detail=response_detail)
            if response_detail == "summary":
                context = self._sign_context_guards(context, session, self._components()[0])
            result.update(handoff=handoff, edit_context=context)
        latest = self._owned_session(session_id, include_baseline=False)
        if (latest.get("owner_revision", 0) != session.get("owner_revision", 0)
                or latest["state"] != session["state"] or latest.get("plan_id") != session.get("plan_id")
                or latest.get("transaction_id") != session.get("transaction_id")):
            return {**failure, "reason_code": "session_changed_during_retry",
                    "message": "会话在核对期间变化，请读取当前状态后继续。"}
        return result

    def inspect_component_contract(self, component_type: str) -> dict[str, Any]:
        """Read compiled event signatures; this does not verify a page instance."""
        if not isinstance(component_type, str) or not component_type.strip():
            raise EditorServiceError("component_type is required")
        _, _, _, compiler, _ = self._components()
        return compiler.describe_component_contract(component_type)

    @component_request
    def find_edit_nodes(self, session_id: str, *, kind: str = "nodes", query: str = "", group_key: str = "",
                        element_key: str = "", offset: int = 0, limit: int = 30) -> dict[str, Any]:
        from .node_discovery import find_baseline_nodes
        session = self._owned_session(session_id)
        if session["state"] not in {"locked", "planned", "awaiting_approval"}:
            raise EditorServiceError("Node discovery requires an active edit session")
        _, _, repository, compiler, _ = self._components()
        current = repository.load_draft_by_id(session["design_id"])
        if current["content_hash"] != session["baseline"]["content_hash"]:
            raise EditorServiceError("Draft changed after the edit session began; node discovery is stale")
        current_relations = repository.load_relations(session["design_id"])
        if relation_snapshot_hash(current_relations) != relation_snapshot_hash(session["baseline"].get("relations") or []):
            raise EditorServiceError("Draft relations changed after the edit session began; node discovery is stale")
        registry, binding = compiler.registry_and_binding()
        if binding["compiler_binding_sha256"] != session["compiler_hash"]:
            raise EditorServiceError("Frontend compiler binding changed; node discovery is stale")
        result = find_baseline_nodes(self._compiler_design(session["baseline"]), registry, kind=kind,
                                    query=query, group_key=group_key, element_key=element_key, offset=offset, limit=limit)
        latest = self._owned_session(session_id, include_baseline=False)
        if latest.get("owner_revision", 0) != session.get("owner_revision", 0):
            raise EditorServiceError("Session ownership changed while discovering nodes")
        return {**result, "session_id": session_id, "backup_id": session["backup_id"],
                "design_id": session["design_id"], "baseline_content_hash": session["baseline"]["content_hash"],
                "compiler_binding": binding, "write_executed": False,
                "context_binding": {"session_id": session_id, "owner_revision": session.get("owner_revision", 0),
                    "baseline_content_hash": session["baseline"]["content_hash"],
                    "relation_sha256": relation_snapshot_hash(current_relations), "compiler_hash": session["compiler_hash"]}}

    def _sign_context_guards(self, context: dict[str, Any], session: dict[str, Any], config) -> dict[str, Any]:
        from .edit_guards import create_guard_ref, parameter_value
        design = self._compiler_design(session["baseline"])
        secret = approval_secret(config)
        for node in context["nodes"]:
            for row in node["edit_options"]["parameter_operations"]:
                operation = row["template"]
                value = parameter_value(design, node["group_key"], node["node_key"], operation["path"], operation.get("item_key"))
                operation["guard_ref"] = create_guard_ref(session, self.conversation_id, operation, value, secret)
                row.pop("expected_old_value_ref", None)
                row["required_inputs"] = ["value"]
            for descriptor in node.get("parameters", []):
                descriptor["read_value"]["arguments"]["session_id"] = session["id"]
            if "full_context" in node:
                node["full_context"]["arguments"]["session_id"] = session["id"]
        context["context_binding"] = {"session_id": session["id"], "owner_revision": session.get("owner_revision", 0),
            "baseline_content_hash": session["baseline"]["content_hash"],
            "relation_sha256": relation_snapshot_hash(session["baseline"].get("relations") or []),
            "compiler_hash": session["compiler_hash"]}
        return context

    @component_request
    def inspect_edit_context(self, session_id: str, targets: list[dict[str, str]], *, operation_cursor: int = 0,
                             include_context: bool | None = None, response_detail: str = "full",
                             parameter_paths: list[str] | None = None) -> dict[str, Any]:
        from .edit_context import build_edit_context
        session = self._owned_session(session_id)
        if session["state"] not in {"locked", "planned", "awaiting_approval"}:
            raise EditorServiceError("Target inspection requires an active edit session")
        config, _, repository, compiler, _ = self._components()
        current = repository.load_draft_by_id(session["design_id"])
        if current["content_hash"] != session["baseline"]["content_hash"]:
            raise EditorServiceError("Draft changed after the edit session began; target evidence is stale")
        current_relations = repository.load_relations(session["design_id"])
        if relation_snapshot_hash(current_relations) != relation_snapshot_hash(session["baseline"].get("relations") or []):
            raise EditorServiceError("Draft relations changed after the edit session began; target evidence is stale")
        registry, binding = compiler.registry_and_binding()
        if binding["compiler_binding_sha256"] != session["compiler_hash"]:
            raise EditorServiceError("Frontend compiler binding changed; target contracts are stale")
        result = build_edit_context(self._compiler_design(session["baseline"]), registry, targets,
                                    operation_cursor=operation_cursor, include_context=include_context,
                                    response_detail=response_detail, parameter_paths=parameter_paths)
        if result["context_detail"] == "summary":
            result = self._sign_context_guards(result, session, config)
        # Ownership may change while native source checks are running.
        latest = self._owned_session(session_id, include_baseline=False)
        if latest.get("owner_revision", 0) != session.get("owner_revision", 0):
            raise EditorServiceError("Session ownership changed while reading the edit context")
        response = {**result, "session_id": session_id, "backup_id": session["backup_id"],
                "design_id": session["design_id"], "baseline_content_hash": session["baseline"]["content_hash"],
                "compiler_binding": binding, "write_executed": False}
        response["context_binding"] = {"session_id": session_id, "owner_revision": session.get("owner_revision", 0),
            "baseline_content_hash": session["baseline"]["content_hash"],
            "relation_sha256": relation_snapshot_hash(current_relations), "compiler_hash": session["compiler_hash"]}
        if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > 1024 * 1024:
            error = EditorServiceError("Edit context exceeds 1 MiB; request fewer target nodes or include_context=false")
            error.details = {"phase": "edit_context", "reason_code": "edit_context_response_limit",
                             "max_output_bytes": 1024 * 1024,
                             "next_step": "Request fewer targets or include_context=false; no parameter or guard was truncated."}
            raise error
        return response

    @component_request
    def read_edit_value(self, session_id: str, group_key: str, node_key: str, path: str, *,
                        item_key: dict[str, Any] | None = None, offset: int = 0,
                        max_output_bytes: int = 16384) -> dict[str, Any]:
        from .edit_guards import create_guard_ref, parameter_value
        from .value_transport import value_page
        if type(max_output_bytes) is not int or not 4096 <= max_output_bytes <= 65536:
            raise EditorServiceError("max_output_bytes must be between 4096 and 65536")
        # The inspection performs the complete native canvas validation and all
        # ownership, live draft, relation and compiler checks on every page.
        context = self.inspect_edit_context(session_id, [{"group_key": group_key, "node_key": node_key}],
            response_detail="summary", parameter_paths=[path])
        session = self._owned_session(session_id)
        if session.get("owner_revision", 0) != context["context_binding"]["owner_revision"]:
            raise EditorServiceError("Session ownership changed while reading a value")
        node = context["nodes"][0]
        value = parameter_value(self._compiler_design(session["baseline"]), group_key, node_key, path, item_key)
        operation = {"operation": "set_array_item" if item_key is not None else "set_parameter",
            "action_group_key": group_key, "target_node_key": node_key, "expected_element_key": node["element_key"],
            "path": path, **({"item_key": item_key} if item_key is not None else {})}
        metadata = {"session_id": session_id, "group_key": group_key, "node_key": node_key, "path": path,
            "item_key": item_key, "context_binding": context["context_binding"],
            "guard_ref": create_guard_ref(session, self.conversation_id, operation, value, approval_secret(self._components()[0])),
            "read_only": True, "write_executed": False}
        overhead = len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) + 1024
        if max_output_bytes - overhead < 1024:
            raise EditorServiceError("Byte budget is too small for this target's exact binding; request a larger max_output_bytes")
        page = value_page(value, offset=offset, max_output_bytes=max_output_bytes - overhead)
        latest = self._owned_session(session_id, include_baseline=False)
        if (latest.get("owner_revision", 0) != session.get("owner_revision", 0)
                or latest["state"] not in {"locked", "planned", "awaiting_approval"}):
            raise EditorServiceError("Session ownership or active state changed while reading a value")
        result = {**metadata, **page, "max_output_bytes": max_output_bytes}
        if not page.get("complete"):
            from .evidence_guidance import guidance
            result.update(guidance("value_incomplete", "complete_old_value",
                next_read={"tool": "read_edit_value", "arguments": {"offset": page.get("next_offset")},
                           "reuse_original_arguments": True} if page.get("next_offset") is not None else None,
                can_continue=("signed_guard_reference",), stop_reason="required_value_fragment_unread"
                if page.get("next_offset") is not None else "verify_fragment_assembly"))
        return result

    def read_plan_review(self, plan_id: str, *, section="summary", offset=0, limit=10, item_id="", detail="full",
                         field_path="", value_offset=0, max_output_bytes=16384):
        plan = self.state.get_plan(plan_id)
        session = self._owned_session(plan["session_id"])
        artifact = load_artifact(plan["artifact_path"])
        self._validate_plan_review(plan, session, artifact)
        report = artifact["business_review"]
        page = review_page(report, section, offset, limit, item_id, detail=detail, field_path=field_path,
                           value_offset=value_offset, max_output_bytes=max_output_bytes)
        latest = self._owned_session(session["id"], include_baseline=False)
        if latest.get("owner_revision", 0) != session.get("owner_revision", 0):
            raise EditorServiceError("Session ownership changed while reading the plan report")
        return {"plan_id": plan_id, "report_sha256": report["review_sha256"], "read_only": True,
                "current_plan": latest.get("plan_id") == plan_id,
                **page, "write_executed": False}

    def plan_canvas_patch(
        self, session_id: str, operations: list[dict[str, Any]], review_spec: dict[str, Any] | None = None,
        *, response_detail: str = "full", generation_mode: str = "full",
    ) -> dict[str, Any]:
        session = self._owned_session(session_id)
        if session.get("session_mode", "edit") != "edit":
            raise EditorServiceError("Reference repair sessions require plan_reference_repairs")
        return self._plan_patch(session_id, operations, review_spec=review_spec, response_detail=response_detail,
                                generation_mode=generation_mode)

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
        registry, binding = compiler.registry_and_binding()
        if binding["compiler_binding_sha256"] != session["compiler_hash"]:
            raise EditorServiceError("Frontend compiler binding changed after the repair session began")
        design = self._compiler_design(session["baseline"])
        return design, scan_references(design, registry), compiler

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

    def _resolve_review_spec(self, session: dict[str, Any], supplied: dict[str, Any] | None) -> tuple[dict[str, Any], str]:
        previous = None
        checkpoint = self.state.review_spec_checkpoint(session["id"], conversation_id=self.conversation_id,
                                                       owner_revision=session.get("owner_revision", 0))
        if checkpoint is not None:
            previous = checkpoint["spec"]
        elif session.get("plan_id"):
            old_plan = self.state.get_plan(session["plan_id"])
            try:
                old_artifact = load_artifact(old_plan["artifact_path"])
            except (OSError, ValueError) as exc:
                error = EditorServiceError("旧计划没有独立要求记录且候选文件不可读；请确认取消旧计划后，以完整修改范围和场景建立新会话。")
                error.details = {"phase": "review_requirements", "reason_code": "legacy_review_spec_unavailable",
                                 "session_id": session["id"], "plan_id": session["plan_id"],
                                 "next_step": "inspect_session_cancellation; confirmed cancel; begin_edit_session with original complete scope"}
                raise error from exc
            if old_artifact.get("business_review") is not None:
                self._validate_plan_review(old_plan, session, old_artifact)
                previous = old_artifact["business_review"]["spec"]
            elif old_plan.get("new_hash") != sha256_json(old_artifact.get("hashes")):
                raise EditorServiceError("已有计划的业务验收证据被移除或损坏，不能清空预期后重试。")
        if supplied is None and previous is not None:
            if "scope_policy" not in previous:
                raise EditorServiceError("旧计划未声明修改范围策略，请提交新的验收规则后重新规划。")
            spec = previous
        else:
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
        checkpoint_sha = self.state.save_review_spec_checkpoint(session["id"], spec,
            expected_sha256=checkpoint["sha256"] if checkpoint else None,
            conversation_id=self.conversation_id, owner_revision=session.get("owner_revision", 0))
        return spec, checkpoint_sha

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
        review_spec: dict[str, Any] | None = None, response_detail: str = "full", generation_mode: str = "full",
    ) -> dict[str, Any]:
        if response_detail not in {"summary", "full"}:
            raise EditorServiceError("response_detail must be summary or full")
        if generation_mode not in ("full", "incremental", "auto"):
            raise EditorServiceError("generation_mode must be full, incremental, or auto")
        config, _, repository, compiler, runtime_validator = self._components()
        session = self._owned_session(session_id)
        if session["state"] not in {"locked", "planned"}:
            raise StateError(f"Session state {session['state']!r} cannot be planned")
        current = repository.load_draft_by_id(session["design_id"])
        if current["content_hash"] != session["baseline"]["content_hash"]:
            raise EditorServiceError("Draft changed after the edit session began")
        current_relations = repository.load_relations(session["design_id"])
        if relation_snapshot_hash(current_relations) != relation_snapshot_hash(session["baseline"].get("relations") or []):
            raise EditorServiceError("Draft relation data changed after the edit session began")
        spec, checkpoint_sha = self._resolve_review_spec(session, review_spec)
        guarded = type(operations) is list and any(type(op) is dict and "guard_ref" in op for op in operations)
        if guarded:
            from .edit_guards import expand_guard_operations
            registry, binding = compiler.registry_and_binding()
            if binding["compiler_binding_sha256"] != session["compiler_hash"]:
                raise EditorServiceError("Frontend compiler source or bridge changed after the edit session began")
            operations = expand_guard_operations(session, self.conversation_id, self._compiler_design(session["baseline"]),
                                                  operations, approval_secret(config))
        if repaired is None:
            CompilerBridge.preflight_operations(self._compiler_design(session["baseline"]), operations)
        if repaired is None and type(operations) is list and operations and CompilerBridge.guarded_no_changes(
                self._compiler_design(session["baseline"]), operations):
            return self._no_changes_response(session)
        if not guarded:
            registry, binding = compiler.registry_and_binding()
        if binding["compiler_binding_sha256"] != session["compiler_hash"]:
            raise EditorServiceError("Frontend compiler source or bridge changed after the edit session began")
        generation_baseline = {
            "baseline_design": self._compiler_design(session["baseline"]),
            "baseline_artifacts": self._baseline_codegen_artifacts(session["baseline"]),
        }
        result = (compiler.compile_reference_repair(repaired, **generation_baseline) if repaired is not None else
                  compiler.compile_patch(generation_baseline["baseline_design"], operations,
                                         baseline_artifacts=generation_baseline["baseline_artifacts"],
                                         generation_mode=generation_mode))
        if result.get("no_change") is True:
            return self._no_changes_response(session)
        compiler_evidence = compiler.verify_compiled_result(result, **generation_baseline, generation_mode=generation_mode)
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
        with stage("review.scenarios"):
            scenario_report = LogicScenarioRunner(config).run_pair(
                str(session["baseline"].get("js_code") or ""), desired["js_code"], spec["scenarios"],
            )
        with stage("review.business_checks"):
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
            "generation_mode": generation_mode,
            **({"generation_selection": compiler_evidence["generation_selection"]} if compiler_evidence.get("generation_selection") else {}),
            **({"generation": compiler_evidence["generation"]} if compiler_evidence.get("generation") else {}),
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
            expected_review_checkpoint=checkpoint_sha,
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
            **({"generation_selection": plan_artifact["generation_selection"] if response_detail == "full" else
                self._generation_summary(plan_artifact["generation_selection"])} if plan_artifact.get("generation_selection") else {}),
            **({"generation": plan_artifact["generation"] if response_detail == "full" else
                self._generation_summary(plan_artifact["generation"])} if plan_artifact.get("generation") else {}),
            "baseline_diagnostics": plan_artifact["event_binding_validation"].get("baseline_diagnostics", [])[:5],
            "baseline_diagnostic_count": len(plan_artifact["event_binding_validation"].get("baseline_diagnostics", [])),
            "unreachable_diagnostics": plan_artifact["event_binding_validation"].get("unreachable_diagnostics", [])[:5],
            "unreachable_diagnostic_count": len(plan_artifact["event_binding_validation"].get("unreachable_diagnostics", [])),
            "event_parameter_diagnostics": [{"binding_node_key": check["binding_node_key"],
                "target_group_key": check["target_group_key"], "issues": check["parameter_issues"]}
                for check in plan_artifact["event_binding_validation"].get("checks", []) if check.get("parameter_issues")][:5],
            "event_binding_diagnostics": [check for check in plan_artifact["event_binding_validation"].get("checks", []) if check.get("binding_issue")][:5],
            "event_binding_diagnostic_count": sum(bool(check.get("binding_issue")) for check in plan_artifact["event_binding_validation"].get("checks", [])),
            "canvas_diagnostics": plan_artifact["event_binding_validation"].get("canvas_diagnostics", [])[:5],
            "canvas_diagnostic_count": len(plan_artifact["event_binding_validation"].get("canvas_diagnostics", [])),
            "hashes": plan_artifact["hashes"],
            "write_executed": False,
            **({"reference_repairs": plan_artifact["reference_repairs"]} if response_detail == "full" else
               {"reference_repair_count": len(plan_artifact["reference_repairs"])}),
            **review_response(business_review, plan_id, response_detail),
            "ready_for_approval": business_review["status"] != "failed",
            "blocked_reasons": ["业务验收未通过，请按 review_issues 查看失败的配置、范围或场景证据。"] if business_review["status"] == "failed" else [],
        }

    @staticmethod
    def _generation_summary(value):
        if type(value) is dict:
            return {key: EditorService._generation_summary(item) for key, item in value.items()}
        if type(value) is list:
            return {"count": len(value), "preview": value[:5], "complete": len(value) <= 5}
        return value

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
        from .ui_launch import inspect_window_startup, launch_window, recover_stalled_window
        if response_detail not in {"summary", "full"}:
            raise EditorServiceError("response_detail must be summary or full")
        plan = self.state.get_plan(plan_id)
        session = self._owned_session(plan["session_id"])
        artifact = load_artifact(plan["artifact_path"])
        self._validate_plan_review(plan, session, artifact)
        report = artifact["business_review"]
        review_dir = runtime_root() / "plan-reviews"
        review_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "plan_id": plan_id, "business_review": report,
            "action_context": action_context(session["baseline"]),
            "before": session["baseline"].get("data_json") or json.loads(session["baseline"]["data"]),
            "after": json.loads(artifact["desired"]["data"]), "validation": artifact.get("validation"),
        }
        snapshot_sha = sha256_json(snapshot)
        review_path = review_dir / f"{plan_id}-{snapshot_sha}.json"
        try:
            existing_snapshot = load_artifact(review_path)
        except (OSError, ValueError):
            existing_snapshot = None
        if existing_snapshot != snapshot:
            save_artifact(review_path, snapshot)
        plugin_root = Path(__file__).resolve().parents[2]
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        launch_key = f"plan-review:{plan_id}:{snapshot_sha}:{self.conversation_id}:{session.get('owner_revision', 0)}"
        try:
            startup = inspect_window_startup(launch_key)
            recovery = {}
            if startup.get("startup_status") in {"startup_timeout", "heartbeat_timeout"}:
                recovery = recover_stalled_window(launch_key, startup["startup_id"])
            opened = launch_window(executable=pythonw if pythonw.exists() else python,
                script=plugin_root / "scripts" / "approval_app.py", arguments=["--plan-review", str(review_path)],
                cwd=plugin_root, launch_key=launch_key, recovery_kind="nonwriting", recovery_artifact_path=review_path)
            window = {"review_window_opened": opened.get("window_opened", False),
                      "review_window_starting": opened.get("window_starting", False), "window_startup": opened,
                      **({"window_recovery": recovery} if recovery else {}),
                      **({"review_window_error": opened["window_error"]} if opened.get("window_error") else {})}
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

    def _approval_queue_id(self, session: dict[str, Any]) -> str:
        queue_id = session.get("queue_id") or ""
        if queue_id:
            try:
                items = self.state.edit_queue(queue_id, self.conversation_id)
            except (StateError, ValueError, TypeError):
                items = []
            if not any(item["id"] == session["id"] and item["queued_owner_revision"] == session.get("owner_revision", 0)
                       for item in items):
                queue_id = ""
        return queue_id

    def _approval_launch_key(self, session: dict[str, Any], transaction_id: str) -> str:
        queue_id = self._approval_queue_id(session)
        return (f"approval-queue:{queue_id}:{self.conversation_id}" if queue_id else
                f"approval:{transaction_id}:{self.conversation_id}:{session.get('owner_revision', 0)}")

    @staticmethod
    def _handling_options(session: dict[str, Any]) -> dict[str, Any]:
        return {"session_id": session.get("id"), "inspect_tool": "inspect_session_cancellation",
                "cancel_unexecuted_tool": "cancel_unexecuted_session", "local_release_tool": "release_local_session",
                "requires_user_confirmation": True,
                "message": "先查看具体处理范围。可取消未执行审批；结果不明或无法核对时，可经确认保留数据并释放本地流程。"}

    @staticmethod
    def _approval_launch_response(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            result = {"window_opened": False, "window_starting": False, "window_error": "未收到窗口启动回执。"}
        return {"approval_window_opened": result.get("window_opened") is True,
                "approval_window_starting": result.get("window_starting") is True,
                "window_startup": result,
                **({"approval_window_error": result["window_error"]} if result.get("window_error") else {}),
                **({"already_open": True} if result.get("already_open") else {})}

    def _launch_approval(self, transaction_id: str, *, owner_revision: int | None = None) -> dict[str, Any]:
        from .execution_lock import approval_queue_lock, approval_window_lock
        from .ui_launch import launch_window
        transaction = self.state.get_transaction(transaction_id)
        session = self._owned_session(transaction["session_id"], include_baseline=False)
        revision = session.get("owner_revision", 0)
        if owner_revision is not None and owner_revision != revision:
            raise EditorServiceError("会话归属已变化，原请求不能打开审批。")
        queue_id = self._approval_queue_id(session)
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
        return launch_window(executable=executable, script=script, arguments=arguments, cwd=plugin_root,
                             launch_key=self._approval_launch_key(session, transaction_id),
                             recovery_kind="queue" if queue_id else "approval",
                             recovery_artifact_path=None if queue_id else transaction["artifact_path"])

    def _try_launch_approval(self, transaction_id: str, *, owner_revision: int | None = None) -> dict[str, Any]:
        from .execution_lock import ApprovalWindowBusyError
        try:
            result = self._launch_approval(transaction_id, owner_revision=owner_revision)
            return self._approval_launch_response(result)
        except ApprovalWindowBusyError as exc:
            from .ui_launch import inspect_window_startup
            transaction = self.state.get_transaction(transaction_id)
            session = self._owned_session(transaction["session_id"], include_baseline=False)
            result = inspect_window_startup(self._approval_launch_key(session, transaction_id))
            response = self._approval_launch_response(result)
            return {**response, "already_open": True, "message": str(exc),
                    **({"approval_window_error": "已有窗口占用，但未确认其可用；请关闭原审批窗口后处理旧流程。"}
                       if not response["approval_window_opened"] and not response["approval_window_starting"] else {})}
        except OSError as exc:
            return {"approval_window_opened": False, "approval_window_starting": False, "approval_window_error": str(exc),
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
        checkpoint = self.state.review_spec_checkpoint(session["id"], conversation_id=self.conversation_id,
                                                      owner_revision=session.get("owner_revision", 0))
        if checkpoint is not None and sha256_json(checkpoint["spec"]) != sha256_json(artifact["business_review"]["spec"]):
            raise EditorServiceError("已确认要求已变化；旧计划只可查看，请按当前要求重新生成计划。")
        status = self._readiness_status(probe_runtime=False, defer_compiler=True)
        if not status["noncompiler_write_ready"]:
            raise EditorServiceError("Database write safety gates are not ready: " + json.dumps(status["gates"], ensure_ascii=False))
        config, _, repository, compiler, runtime_validator = self._components()
        database_safety = self._database_safety_binding(config, repository, verified_grants=status.get("grants"))
        if plan["status"] != "ready" or session["state"] != "planned" or session.get("plan_id") != plan_id:
            raise EditorServiceError("Only the current ready plan can enter approval")
        with stage("database.load_draft"):
            current = repository.load_draft_by_id(session["design_id"])
        if (
            current["content_hash"] != artifact.get("base_content_hash")
            or plan.get("base_hash") != artifact.get("base_content_hash")
        ):
            raise EditorServiceError("Draft changed after the plan was generated")
        with stage("database.load_relations"):
            current_relations = repository.load_relations(session["design_id"])
        if (artifact.get("generation") or artifact.get("generation_selection")) and relation_snapshot_hash(current_relations) != relation_snapshot_hash(session["baseline"].get("relations") or []):
            raise EditorServiceError("Draft relation data changed after generation planning")
        registry, binding = compiler.registry_and_binding()
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
            if scan_references(candidate_design, registry)["summary"]["total"]:
                raise EditorServiceError("Candidate still contains invalid structured variable references")
            repair_report = scan_references(self._compiler_design(session["baseline"]), registry)
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
            candidate_design, desired, baseline_design=self._compiler_design(session["baseline"]),
            **({"baseline_artifacts": self._baseline_codegen_artifacts(session["baseline"])}
               if artifact.get("generation") or artifact.get("generation_selection") else {}),
            generation_mode=artifact.get("generation_mode", "full"))
        if (
            compiler_evidence.get("validation") != artifact.get("validation")
            or compiler_evidence.get("event_binding_validation") != artifact.get("event_binding_validation")
            or compiler_evidence.get("interface_metadata") != artifact.get("interface_metadata")
            or compiler_evidence.get("generation") != artifact.get("generation")
            or compiler_evidence.get("generation_selection") != artifact.get("generation_selection")
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
            current_relations=current_relations,
            desired_relations=desired["relation_data"],
            database_safety=database_safety,
            runtime_validation=runtime_validation,
            compiler_evidence={
                "compiler_binding": binding,
                "validation": compiler_evidence["validation"],
                "event_binding_validation": compiler_evidence.get("event_binding_validation"),
                "code_generation_complete": True,
                "generated_code_consistency_verified": compiler_evidence["generated_code_consistency_verified"],
                **({"generation": compiler_evidence["generation"]} if compiler_evidence.get("generation") else {}),
                **({"generation_selection": compiler_evidence["generation_selection"]} if compiler_evidence.get("generation_selection") else {}),
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
                                      conversation_id=self.conversation_id, owner_revision=session.get("owner_revision", 0),
                                      expected_review_checkpoint=checkpoint["sha256"] if checkpoint is not None else None)
        approval_window = self._try_launch_approval(transaction_id, owner_revision=session.get("owner_revision", 0))
        return {
            "transaction_id": transaction_id,
            "state": "awaiting_approval",
            "queue_id": session.get("queue_id") or None,
            "statement_count": len(transaction_artifact["statements"]),
            **approval_window,
            "handling_options": self._handling_options(session),
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

    def _graph_review(self, session: dict[str, Any], transaction: dict[str, Any]) -> dict[str, Any] | None:
        if look_graph_review(session, transaction) is None:
            return None
        return look_graph_review(session, transaction, self.state.graph_review(transaction["id"]))

    def record_graph_review(self, transaction_id: str, outcome: str, *, expected_revision: int = 0,
                            proposal_sha256: str = "", graph_revision: int | None = None,
                            reconsider_declined: bool = False) -> dict[str, Any]:
        transaction = self.state.get_transaction(transaction_id)
        session = self._owned_session(transaction["session_id"], include_baseline=False, check_database=False)
        self.state.record_graph_review(transaction_id, outcome, conversation_id=self.conversation_id,
            expected_revision=expected_revision, proposal_sha256=proposal_sha256, graph_revision=graph_revision,
            reconsider_declined=reconsider_declined)
        # Re-read ownership and the pointer after the atomic metadata update.
        session = self._owned_session(session["id"], include_baseline=False, check_database=False)
        return {"transaction_id": transaction_id, "look_graph_review": self._graph_review(session, transaction),
                "consent_granted": False, "graph_write_executed": False, "write_executed": False}

    def get_write_status(self, transaction_id: str) -> dict[str, Any]:
        from .ui_launch import inspect_window_startup
        transaction = self.state.get_transaction(transaction_id)
        session = self._owned_session(transaction["session_id"], include_baseline=False, check_database=False)
        artifact_error = None
        try:
            artifact = load_artifact(transaction["artifact_path"])
            if not isinstance(artifact, dict):
                raise ValueError("审批文件不是有效对象")
        except (OSError, ValueError) as exc:
            artifact = {}
            artifact_error = str(exc)
        validation_error = artifact_error
        if validation_error is None:
            try:
                validate_transaction_artifact(artifact)
                require_write_business_review(artifact)
            except Exception as exc:
                validation_error = str(exc)
        context = artifact.get("action_context")
        publication_targets = [publication_target(context if isinstance(context, dict) else None, session)] if (
            session.get("transaction_id") == transaction_id and transaction["state"] == "saved"
            and session["state"] == "finished") else []
        window_startup = inspect_window_startup(self._approval_launch_key(session, transaction_id))
        return {
            "transaction_id": transaction_id,
            "kind": transaction["kind"],
            "state": transaction["state"],
            "message": transaction["message"],
            "artifact_available": artifact_error is None,
            "artifact_error": artifact_error,
            "artifact_valid": validation_error is None,
            "artifact_validation_error": validation_error,
            "handling_available": True,
            "handling_options": self._handling_options(session),
            "window_startup": window_startup,
            **write_status_guidance(transaction, session, window_startup, artifact_valid=validation_error is None),
            "statement_count": len(artifact["statements"]) if isinstance(artifact.get("statements"), list) else 0,
            "approved_count": len(artifact["approvals"]) if isinstance(artifact.get("approvals"), dict) else 0,
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
            "look_graph_review": self._graph_review(session, transaction),
        }

    def reopen_approval(self, transaction_id: str, *, owner_revision: int | None = None) -> dict[str, Any]:
        transaction = self.state.get_transaction(transaction_id)
        session = self._owned_session(transaction["session_id"], include_baseline=False)
        if owner_revision is not None and session.get("owner_revision", 0) != owner_revision:
            raise EditorServiceError("会话归属已变化，原窗口不能重新打开审批。")
        failure = {"transaction_id": transaction_id, "approval_window_opened": False,
                   "approval_window_starting": False, "write_executed": False,
                   "handling_options": self._handling_options(session)}
        if (transaction["state"] != "awaiting_approval" or session.get("transaction_id") != transaction_id
                or session["state"] not in {"awaiting_approval", "restore_pending"}):
            return {**failure, "reason_code": "approval_not_pending",
                    "approval_window_error": "只有当前尚未执行的审批可以重新打开；请检查状态并选择取消或结果核对。"}
        try:
            artifact = load_artifact(transaction["artifact_path"])
            validate_transaction_artifact(artifact)
            require_write_business_review(artifact)
            if (artifact.get("transaction_id") != transaction_id
                    or artifact.get("session_id") != transaction["session_id"]
                    or artifact.get("design_id") != session.get("design_id")):
                raise EditorServiceError("审批文件与当前事务或会话身份不匹配。")
            if artifact.get("execution_started") or artifact.get("executed"):
                return {**failure, "reason_code": "execution_result_uncertain",
                        "approval_window_error": "此审批已有执行标记，不能重复打开执行；请核对结果或经确认释放本地流程。"}
        except Exception as exc:
            return {**failure, "reason_code": "approval_artifact_unavailable",
                    "approval_window_error": str(exc), "retry_requires_new_plan": True}
        from .ui_launch import inspect_window_startup, recover_stalled_window
        launch_key = self._approval_launch_key(session, transaction_id)
        startup = inspect_window_startup(launch_key)
        recovery = {}
        if startup.get("startup_status") in {"startup_timeout", "heartbeat_timeout"}:
            def verify_unexecuted(bound_path):
                latest_tx = self.state.get_transaction(transaction_id)
                latest = self._owned_session(session["id"], include_baseline=False)
                if (latest_tx["state"] != "awaiting_approval" or latest.get("transaction_id") != transaction_id
                        or latest.get("owner_revision", 0) != session.get("owner_revision", 0)
                        or Path(latest_tx["artifact_path"]).resolve() != bound_path.resolve()):
                    return False
                candidate = load_artifact(bound_path)
                return (candidate.get("transaction_id") == transaction_id
                        and candidate.get("session_id") == session["id"]
                        and not candidate.get("execution_started") and not candidate.get("executed"))
            recovery = recover_stalled_window(launch_key, startup["startup_id"], verify_unexecuted=verify_unexecuted)
            if not recovery.get("window_recovered"):
                return {**failure, "reason_code": "stalled_window_recovery_refused", "window_recovery": recovery,
                        "approval_window_error": recovery.get("window_error", "窗口恢复未完成，请检查当前流程状态。")}
        window = self._try_launch_approval(transaction_id, owner_revision=session.get("owner_revision", 0))
        return {**failure, **window, **({"window_recovery": recovery} if recovery else {})}

    def recover_edit_session(self, session_id: str) -> dict[str, Any]:
        from .execution_lock import ExecutionBusyError
        session = self._owned_session(session_id, include_baseline=False)
        try:
            return recover_session(session_id, state=self.state, conversation_id=self.conversation_id,
                                   owner_revision=session.get("owner_revision", 0))
        except Exception as exc:
            return {"session_id": session_id, "outcome": "recovery_blocked", "verified": False,
                    "session_released": False, "business_data_written": False,
                    "reason_code": "executor_busy" if isinstance(exc, ExecutionBusyError) else "recovery_unavailable",
                    "error": str(exc), "handling_options": self._handling_options(session)}

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
                    "look_graph_review": self._graph_review(session, transaction),
                    "publication_targets": [action_context(session["baseline"])]}
        if session["state"] == "finished" and transaction["state"] == "saved":
            return {"verified": True, "session_released": True, "design_id": session["design_id"],
                    "ref_id": session["ref_id"], "verification_scope": "previously_finished_session",
                    "current_draft_rechecked": False, "runtime_finalized": False,
                    "manual_publication_required": True,
                    "look_graph_review": self._graph_review(session, transaction),
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
        completed = {**session, "state": "finished"}
        transaction = self.state.get_transaction(session["transaction_id"]) if session.get("transaction_id") else {}
        return {
            "finished": True,
            "look_graph_review": self._graph_review(completed, transaction),
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
        release_available = session["state"] in ACTIVE_STATES
        local_release = {"available": release_available,
                "fingerprint": sha256_json({"release_local_session": snapshot["fingerprint"]}) if release_available else None,
                "requires_user_confirmation": release_available, "requires_idle_executor": True,
                "reason": None if release_available else "会话已经结束，无需释放。",
                "meaning": "保留数据库当前内容，只归档并释放本地流程；不核对或标记保存成功。旧审批不能复用。",
                "backup_id": session["backup_id"],
                "transactions": [{key: row[key] for key in ("id", "kind", "state")} for row in snapshot["transactions"]]}
        return {"session_id": session_id, "action_name": session["action_name"], "action_code": session["action_code"],
                "session_state": session["state"], "cancellable": reason is None, "reason": reason,
                "plans": [{key: row[key] for key in ("id", "status")} for row in snapshot["plans"]
                          if row["status"] in {"ready", "blocked", "frozen"}],
                "approvals": [{key: row[key] for key in ("id", "state")} for row in snapshot["transactions"]
                              if row["state"] == "awaiting_approval"],
                "cancellation_fingerprint": snapshot["fingerprint"] if reason is None else None,
                "requires_user_confirmation": reason is None, "write_executed": False,
                "local_release": local_release,
                "message": "取消会使以上未执行计划和审批失效并释放本地会话；保留当前业务数据、备份和历史记录。"}

    def release_local_session(self, session_id: str, recovery_fingerprint: str, user_confirmation: str) -> dict[str, Any]:
        if (not isinstance(user_confirmation, str) or not user_confirmation.strip() or len(user_confirmation) > 2000
                or not isinstance(recovery_fingerprint, str) or len(recovery_fingerprint) != 64):
            raise EditorServiceError("先展示本地释放范围与未核验含义，再取得用户明确确认；需要确认原文和对应指纹。")
        snapshot = self.state.cancellation_snapshot(session_id, conversation_id=self.conversation_id)
        if sha256_json({"release_local_session": snapshot["fingerprint"]}) != recovery_fingerprint:
            raise EditorServiceError("会话、计划或审批已变化，请重新展示释放范围并取得用户确认。")
        session = snapshot["session"]
        result = archive_edit_session(session_id, state=self.state, conversation_id=self.conversation_id,
            owner_revision=session["owner_revision"], expected_snapshot=session,
            expected_fingerprint=snapshot["fingerprint"], user_confirmation=user_confirmation)
        return {**result, "write_executed": False, "backups_retained": True, "audit_retained": True,
                "retry_requires_new_plan": True,
                "message": "已按用户确认保留当前数据并释放本地流程。原执行结果未核验，旧审批不能再执行。"}

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
        from .ui_launch import launch_window
        session = self._owned_session(session_id, include_baseline=False, check_database=False)
        if session["state"] not in {"locked", "planned", "awaiting_approval", "restore_pending", "saved", "saved_state_pending", "verified"}:
            raise EditorServiceError("会话已经结束，无需释放占用。")
        plugin_root = Path(__file__).resolve().parents[2]
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        try:
            startup = launch_window(executable=pythonw if pythonw.exists() else python,
                script=plugin_root / "scripts" / "abort_session_app.py",
                arguments=[session_id, self.conversation_id, str(session.get("owner_revision", 0))], cwd=plugin_root,
                launch_key=f"session-handling:{session_id}:{self.conversation_id}:{session.get('owner_revision', 0)}")
        except OSError as exc:
            startup = {"window_opened": False, "window_starting": False, "startup_status": "failed", "window_error": str(exc)}
        return {"aborted": False, "session_id": session_id,
                "confirmation_window_opened": startup.get("window_opened") is True,
                "confirmation_window_starting": startup.get("window_starting") is True,
                "window_startup": startup, "handling_options": self._handling_options(session),
                "message": "请选择继续处理或释放本地流程；窗口打不开时可先查看聊天取消或本地释放范围。"}
