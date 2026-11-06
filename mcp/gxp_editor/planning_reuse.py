"""Reuse exact native candidates while independently rerunning every new review.

The caller still reads the live baseline/relations, resolves current review
requirements and installs the next plan with the existing owner/plan CAS. Only
source-bound generation and reference-bound successful compilation are reused.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .approval import load_artifact
from .compiler import CompilerBridge, CompilerError
from .hashing import sha256_json, sha256_text
from .state import StateError


_CANDIDATE_FIELDS = (
    "planning_protocol", "session_id", "action_context", "base_content_hash",
    "compiler_hash", "compiler_binding", "registry_hash", "rule_source_sha256", "frontend_version",
    "operations", "desired", "validation", "generation_mode", "generation_selection", "generation",
    "interface_metadata", "event_binding_validation", "code_generation_complete",
    "generated_code_consistency_verified", "runtime_validation", "diff", "reference_repairs",
)
_COMPILER_EVIDENCE_FIELDS = (
    "compiler_binding", "validation", "event_binding_validation", "code_generation_complete",
    "generated_code_consistency_verified", "interface_metadata", "generation", "generation_selection",
)


def candidate_evidence_hash(artifact: dict[str, Any]) -> str:
    """Bind complete generated artifacts and evidence, independently of review.

    New plans include this digest in their state-bound plan hash. Legacy plans
    have no such commitment and must take the normal generation path. Include
    recomputed artifact hashes as well as stored hashes: editing either cannot
    make a malformed candidate reusable.
    """
    desired = artifact.get("desired")
    desired = desired if isinstance(desired, dict) else {}
    recomputed = {
        "data_sha256": sha256_text(str(desired.get("data") or "")),
        "js_sha256": sha256_text(str(desired.get("js_code") or "")),
        "csharp_sha256": sha256_text(str(desired.get("csharp_code") or "")),
        "relation_sha256": sha256_json(desired.get("relation_data")),
    }
    return sha256_json({"purpose": "planned_candidate_evidence_v1",
        "candidate": {key: artifact[key] for key in _CANDIDATE_FIELDS if key in artifact},
        "recorded_hashes": artifact.get("hashes"), "actual_hashes": recomputed})


def _same_current_session(service, session: dict[str, Any]) -> dict[str, Any]:
    latest = service._owned_session(session["id"], include_baseline=False, check_database=False)
    if (latest.get("plan_id") != session.get("plan_id")
            or latest.get("owner_revision", 0) != session.get("owner_revision", 0)
            or latest.get("state") != "planned"):
        raise StateError("当前计划或会话归属在候选复用期间已变化，请读取当前计划。")
    return latest


def reuse_planned_candidate(service, session: dict[str, Any], current: dict[str, Any], compiler,
                            runtime_validator, operations: list[dict[str, Any]], generation_mode: str,
                            repaired=None, *, actual_binding: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return verified generation evidence, never old scenarios/review/approval.

    Ordinary ineligibility, changed operations/mode and unusable prior runtime
    evidence take the full generation path. Once an eligible plan is present,
    corrupted evidence, source drift or ownership races fail closed.
    """
    if repaired is not None or session.get("session_mode", "edit") == "reference_repair":
        return None
    if session.get("state") != "planned" or not session.get("plan_id"):
        return None
    verify_source = getattr(compiler, "verify_binding", None)
    reuse_runtime = getattr(runtime_validator, "reuse_evidence", None)
    if not callable(verify_source) or not callable(reuse_runtime):
        return None
    _same_current_session(service, session)
    plan = service.state.get_plan(session["plan_id"])
    if plan.get("session_id") != session["id"] or plan.get("status") not in {"ready", "blocked"}:
        raise StateError("只有当前会话的最新可规划候选可以复用。")
    artifact = load_artifact(plan["artifact_path"])
    service._validate_plan_review(plan, session, artifact)
    if artifact.get("planning_protocol") != "composable_v1" or "candidate_evidence_hash" not in artifact:
        return None
    if artifact["candidate_evidence_hash"] != candidate_evidence_hash(artifact):
        raise CompilerError("Prior candidate generation or compilation evidence changed after planning")
    if sha256_json(artifact.get("operations")) != sha256_json(operations) or artifact.get("generation_mode", "full") != generation_mode:
        return None
    baseline_hash = session["baseline"]["content_hash"]
    if (artifact.get("session_id") != session["id"] or plan.get("base_hash") != baseline_hash
            or artifact.get("base_content_hash") != baseline_hash or current.get("content_hash") != baseline_hash
            or str(current.get("id")) != str(session["design_id"])
            or str(current.get("ref_id")) != str(session["ref_id"])):
        raise CompilerError("Prior candidate does not match the current immutable baseline")
    desired = artifact.get("desired")
    if not isinstance(desired, dict) or artifact.get("hashes") != service._desired_hashes(desired):
        raise CompilerError("Prior generated artifact hashes changed after planning")
    expected_binding = artifact.get("compiler_binding")
    if not isinstance(expected_binding, dict):
        raise CompilerError("Prior candidate has no compiler source binding")
    binding_hash = expected_binding.get("compiler_binding_sha256")
    if not binding_hash or any(value != binding_hash for value in (session.get("compiler_hash"), plan.get("compiler_hash"), artifact.get("compiler_hash"))):
        raise CompilerError("Prior candidate compiler identity differs from the session")
    # Always re-read native source bytes; no mtime-only or permanent success cache.
    verified_binding = verify_source(expected_binding)
    if not service._compiler_binding_matches(expected_binding, verified_binding):
        raise CompilerError("Compiler source binding changed after candidate generation")
    if actual_binding is not None and not service._compiler_binding_matches(actual_binding, verified_binding):
        raise CompilerError("Compiler source binding differs from this planning request")
    if artifact.get("code_generation_complete") is not True or artifact.get("generated_code_consistency_verified") is not True:
        raise CompilerError("Prior candidate lacks complete verified generation evidence")
    validation = artifact.get("validation")
    event_validation = artifact.get("event_binding_validation")
    if (not isinstance(validation, dict) or validation.get("valid") is not True
            or not isinstance(event_validation, dict) or not isinstance(event_validation.get("binding_node_keys"), list)):
        raise CompilerError("Prior candidate validation evidence is incomplete")
    design = service._candidate_design(session["baseline"], desired)
    CompilerBridge._validate_generated_artifacts(design, js_code=desired.get("js_code"),
        csharp_code=desired.get("csharp_code"), relation_data=desired.get("relation_data"))
    runtime_validation = reuse_runtime(deepcopy(artifact.get("runtime_validation") or {}), desired["csharp_code"])
    if runtime_validation is None:
        return None
    if (not isinstance(runtime_validation, dict)
            or runtime_validation.get("csharp_sha256") != sha256_text(desired["csharp_code"])):
        raise CompilerError("Reused runtime evidence belongs to different generated C#")
    service._require_candidate_csharp_validation(desired["csharp_code"], runtime_validation)
    # The current reference/code check above is mandatory. Keep its observation
    # (e.g. build_reused) out of the next candidate commitment: the exact
    # originally successful, now rechecked evidence remains the signed proof.
    runtime_validation = deepcopy(artifact["runtime_validation"])
    _same_current_session(service, session)
    latest_plan = service.state.get_plan(plan["id"])
    if latest_plan.get("new_hash") != plan.get("new_hash") or latest_plan.get("status") != plan.get("status"):
        raise StateError("当前候选在证据核对期间已变化，请重新读取。")
    result = {"design": design, "js_code": desired["js_code"], "csharp_code": desired["csharp_code"],
              "relation_data": desired["relation_data"], "diff": artifact.get("diff") or [],
              "reference_repairs": artifact.get("reference_repairs") or []}
    evidence = {key: artifact[key] for key in _COMPILER_EVIDENCE_FIELDS if key in artifact}
    return deepcopy({"result": result, "compiler_evidence": evidence,
                     "runtime_validation": runtime_validation, "reused_from_plan_id": plan["id"]})
