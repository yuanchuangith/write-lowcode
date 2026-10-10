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
from .transaction import build_transaction_artifact


class EditorServiceError(RuntimeError):
    pass


class EditorService:
    def __init__(self):
        self.state = StateStore()

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
            "active_session": self.state.active_session(),
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
        config, _, repository, compiler, runtime_validator = self._components()
        registry = compiler.describe_registry()
        draft = repository.load_draft_by_identifier(identifier)
        supported = {str(item["key"]) for item in registry.get("elements") or [] if item.get("supported", True)}
        used = self._element_keys(draft["data_json"])
        unknown = sorted(used - supported)
        if unknown:
            raise EditorServiceError("Compiler coverage is incomplete for elementKey: " + ", ".join(unknown))
        compiler_design = self._compiler_design(draft)
        validation = compiler.validate_design(compiler_design)
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
            },
            runtime_artifacts,
        )
        binding = compiler.compiler_binding()
        session = self.state.create_session(
            draft,
            backup["backup_id"],
            binding["compiler_binding_sha256"],
        )
        status = self.editor_status()
        return {
            "session_id": session["id"],
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
            "planning_ready": status["planning_ready"],
            "write_ready": status["write_ready"],
            "production_ready": status["production_ready"],
            "failed_gates": {name: gate for name, gate in status["gates"].items() if not gate.get("ready")},
        }

    def plan_canvas_patch(self, session_id: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
        _, _, repository, compiler, runtime_validator = self._components()
        session = self.state.get_session(session_id)
        if session["state"] not in {"locked", "planned"}:
            raise StateError(f"Session state {session['state']!r} cannot be planned")
        current = repository.load_draft_by_id(session["design_id"])
        if current["content_hash"] != session["baseline"]["content_hash"]:
            raise EditorServiceError("Draft changed after the edit session began")
        registry = compiler.describe_registry()
        binding = compiler.compiler_binding()
        if binding["compiler_binding_sha256"] != session["compiler_hash"]:
            raise EditorServiceError("Frontend compiler source or bridge changed after the edit session began")
        result = compiler.compile_patch(self._compiler_design(session["baseline"]), operations)
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
            "code_generation_complete": compiler_evidence["code_generation_complete"],
            "runtime_validation": runtime_validation,
            "diff": result.get("diff") or [],
            "hashes": self._desired_hashes(desired),
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
        )
        return {
            "plan_id": plan_id,
            "session_id": session_id,
            "operation_count": len(operations),
            "diff": plan_artifact["diff"],
            "validation": plan_artifact["validation"],
            "runtime_validation": plan_artifact["runtime_validation"],
            "compiler_binding": binding,
            "code_generation_complete": True,
            "hashes": plan_artifact["hashes"],
            "write_executed": False,
        }

    def _launch_approval(self, transaction_id: str) -> None:
        plugin_root = Path(__file__).resolve().parents[2]
        script = plugin_root / "scripts" / "approval_app.py"
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        executable = pythonw if pythonw.exists() else python
        subprocess.Popen([str(executable), str(script), transaction_id], cwd=plugin_root)

    def prepare_write(self, plan_id: str) -> dict[str, Any]:
        status = self.editor_status()
        if not status["write_ready"]:
            raise EditorServiceError("Database write safety gates are not ready: " + json.dumps(status["gates"], ensure_ascii=False))
        config, _, repository, compiler, runtime_validator = self._components()
        database_safety = self._database_safety_binding(config, repository)
        plan = self.state.get_plan(plan_id)
        session = self.state.get_session(plan["session_id"])
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
        compiler_evidence = compiler.verify_candidate_artifact(candidate_design, desired)
        if (
            compiler_evidence.get("validation") != artifact.get("validation")
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
                "code_generation_complete": True,
                "hashes": hashes,
            },
        )
        tx_dir = runtime_root() / "transactions"
        tx_dir.mkdir(parents=True, exist_ok=True)
        tx_path = tx_dir / f"{transaction_id}.json"
        save_artifact(tx_path, transaction_artifact)
        self.state.create_transaction(session["id"], plan_id, "write", str(tx_path), transaction_id=transaction_id)
        self.state.update_plan_status(plan_id, "frozen")
        self._launch_approval(transaction_id)
        return {
            "transaction_id": transaction_id,
            "state": "awaiting_approval",
            "statement_count": len(transaction_artifact["statements"]),
            "approval_window_opened": True,
            "write_executed": False,
            "runtime_validation": runtime_validation,
            "compiler_evidence": compiler_evidence,
            "manual_publication_required": True,
        }

    def get_write_status(self, transaction_id: str) -> dict[str, Any]:
        transaction = self.state.get_transaction(transaction_id)
        artifact = load_artifact(transaction["artifact_path"])
        return {
            "transaction_id": transaction_id,
            "kind": transaction["kind"],
            "state": transaction["state"],
            "message": transaction["message"],
            "statement_count": len(artifact.get("statements") or []),
            "approved_count": len(artifact.get("approvals") or {}),
            "executed": bool(artifact.get("executed")),
            "old_content_hash": artifact.get("old_content_hash"),
            "new_content_hash": artifact.get("new_content_hash"),
        }

    def prepare_restore(self, session_id: str, backup_id: str) -> dict[str, Any]:
        status = self.editor_status()
        if not status["write_ready"]:
            raise EditorServiceError("Database restore safety gates are not ready")
        config, _, repository, _, runtime_validator = self._components()
        database_safety = self._database_safety_binding(config, repository)
        session = self.state.get_session(session_id)
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
        self.state.create_transaction(session_id, None, "restore", str(tx_path), transaction_id=transaction_id)
        self._launch_approval(transaction_id)
        return {
            "transaction_id": transaction_id,
            "state": "awaiting_approval",
            "statement_count": len(transaction_artifact["statements"]),
            "approval_window_opened": True,
            "runtime_validation": runtime_validation,
            "manual_publication_required": True,
        }

    def verify_saved_draft(self, session_id: str) -> dict[str, Any]:
        _, _, repository, _, _ = self._components()
        session = self.state.get_session(session_id)
        if not session.get("transaction_id"):
            raise EditorServiceError("Session has no write or restore transaction")
        transaction = self.state.get_transaction(session["transaction_id"])
        if transaction["state"] == "saved_state_pending":
            pending_artifact = load_artifact(transaction["artifact_path"])
            pending_current = repository.load_draft_by_id(session["design_id"])
            pending_relations = repository.load_relations(session["design_id"])
            if (
                pending_current["content_hash"] != pending_artifact["new_content_hash"]
                or relation_snapshot_hash(pending_relations)
                != pending_artifact["expected_relation_hash"]
            ):
                raise EditorServiceError(
                    "Database commit state is uncertain and does not match the approved transaction; prepare an approved restore"
                )
            pending_artifact["executed"] = True
            save_artifact(transaction["artifact_path"], pending_artifact)
            self.state.update_transaction(
                transaction["id"], "saved", "Recovered committed transaction state by read-only hash verification"
            )
            self.state.update_session(session_id, "saved")
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
        self.state.update_session(session_id, "verified")
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
        session = self.state.get_session(session_id)
        if session["state"] != "verified":
            raise EditorServiceError("Session must be verified before it can be finished")
        self.state.update_session(session_id, "finished")
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
        session = self.state.get_session(session_id)
        if session["state"] not in {"locked", "planned"}:
            raise EditorServiceError("A session can only be aborted before write approval begins")
        self.state.update_session(session_id, "aborted")
        return {"aborted": True, "session_id": session_id}
