"""Validate readonly evidence before creating any local editing occupancy."""
from __future__ import annotations

import json

from .handoff_store import (HandoffError, HandoffStore, environment_scope, snapshot_targets,
                            compare_targets, design_content_sha256)
from .hashing import sha256_text
from .telemetry import stage


def discover_handoffs(draft, database_config):
    """Return optional local hints; discovery must never prevent ordinary editing."""
    try:
        with stage("handoff.discover"):
            scope = environment_scope(database_config)
            page = HandoffStore().list_page(str(draft["ref_id"]), scope, limit=5, max_bytes=16 * 1024)
            # Public lookup accepts either a RefId or action code. A different
            # action's code can equal this RefId, so enforce exact identity here.
            candidates = [item for item in page["candidates"]
                          if item["action"]["ref_id"] == str(draft["ref_id"])
                          and item["environment_scope"] == scope]
            return {"status": "available", **page, "candidates": candidates,
                    "count": len(candidates), "environment_scope": scope, "selection_required": True,
                    "authorization_inherited": False, "approval_inherited": False,
                    **({"next_read": {"tool": "read_edit_handoff", "arguments": {
                        "identifier": str(draft["ref_id"]), "environment_scope": scope,
                        "cursor": page["next_cursor"], "limit": 5}}} if page["has_more"] else {}),
                    "next_step": "These are optional local evidence summaries, not current snapshot proof. Select a relevant record explicitly before reading it; continue independent inspection when sufficient. Do not automatically select the newest record."}
    except Exception:
        # Local records are optional. Avoid leaking file paths, malformed record
        # values or configuration details through an incidental lookup failure.
        return {"status": "unavailable", "candidates": [], "count": 0,
                "has_more": False, "next_cursor": "", "error_code": "HANDOFF_DISCOVERY_UNAVAILABLE",
                "next_step": "Continue ordinary targeted inspection; local evidence discovery is optional."}


def _stale_details(differences, *, has_semantic_hash):
    target_fields = {"missing", "paramsValue", "element_key", "title"}
    if any(target_fields.intersection(item["changed_fields"]) for item in differences):
        reason = "target_content_changed"
        checks = ["Re-read the changed target parameters and identity, then reassess affected findings and proposals.",
                  "Check the target's relevant parents, group context and dependencies."]
    elif differences:
        reason = "target_context_changed"
        checks = ["Re-read the changed parents, neighbours, group structure or group configuration.",
                  "Reassess findings and proposals that depend on the changed context; unchanged target parameters alone are insufficient."]
    elif has_semantic_hash:
        reason = "other_draft_content_changed"
        checks = ["Target snapshots are unchanged. Inspect other changed groups, global variables and dependencies relevant to the diagnosis before reusing its conclusions."]
    else:
        reason = "legacy_content_changed"
        checks = ["This older record has no semantic content hash. Check whether the raw draft change is formatting or relevant content; unchanged target snapshots do not prove unchanged dependencies."]
    return {"stale_reason": reason, "refresh_scope": {"targets": differences, "checks": checks}}


def inspect_handoff(reference, draft, database_config):
    with stage("handoff.verify"):
        if not isinstance(reference, dict) or set(reference) != {"handoff_id", "sha256"}:
            raise HandoffError("HANDOFF_REF", "handoff_ref requires exactly handoff_id and sha256")
        record = HandoffStore().read(reference["handoff_id"], reference["sha256"])
        if record["environment_scope"] != environment_scope(database_config):
            raise HandoffError("HANDOFF_ENVIRONMENT", "Handoff belongs to a different configured environment")
        if record["action"]["ref_id"] != str(draft["ref_id"]):
            raise HandoffError("HANDOFF_TARGET", "Handoff does not identify the selected active draft")
        if record["action"].get("action_code") and record["action"]["action_code"] != str(draft.get("code") or ""):
            raise HandoffError("HANDOFF_TARGET", "Handoff action code does not match the selected draft")
        targets = [{"group_key": item["group_key"], "node_key": item["node_key"]} for item in record["targets"]]
        # Keep the original requirements and diagnosis available even when a
        # targeted refresh is needed. A stale record is context, not current
        # proof or permission to apply its proposed values.
        context = {"handoff_ref": reference, "targets": targets,
                   "diagnosis_source": record["diagnosis_source"], "change_spec": record["change_spec"],
                   "authorization_inherited": False, "approval_inherited": False}
        if record.get("record_kind") == "diagnosis_only":
            return {**context, "status": "diagnosis_only", "snapshot_reused": False,
                    "requires_current_target_inspection": True,
                    "next_step": "Reuse applicable symptoms, evidence and unresolved questions as historical context. No current draft or target snapshot was captured. Locate and inspect current targets normally; a new export is optional. No cause, proposal, authorization or approval is inferred."}
        if record["draft"]["design_id"] != str(draft["id"]):
            raise HandoffError("HANDOFF_TARGET", "Handoff does not identify the selected active draft")
        data = json.loads(draft["data"])
        current = snapshot_targets(data, targets, allow_missing=True)
        differences = compare_targets(record["targets"], current)
        raw_changed = sha256_text(str(draft.get("data") or "")) != record["draft"]["data_sha256"]
        semantic_hash = record["draft"].get("semantic_sha256")
        semantic_changed = design_content_sha256(draft["data"]) != semantic_hash if semantic_hash else None
        formatting_only = bool(raw_changed and semantic_changed is False and not differences)
        freshness = {"raw_content_changed": raw_changed, "semantic_content_changed": semantic_changed,
                     "formatting_only": formatting_only, "target_snapshot_current": not differences}
        if (raw_changed and not formatting_only) or differences or semantic_changed:
            return {**context, "status": "stale", "target_differences": differences,
                    "draft_content_changed": raw_changed or bool(semantic_changed), "freshness": freshness,
                    **_stale_details(differences, has_semantic_hash=bool(semantic_hash)),
                    "next_step": "Use stale_reason and refresh_scope to assess the saved evidence against the actual changed scope. Analyze or call readonly Look tools for missing facts, then optionally refresh the record or begin ordinary inspection without handoff_ref. No session was created."}
        if record["source_target_differences"]:
            return {**context, "status": "source_target_diverged",
                    "freshness": freshness,
                    "target_differences": record["source_target_differences"],
                    "next_step": "Assess the differing draft locations and dependencies using the saved evidence as context. Analyze or call readonly Look tools as needed; revise unsupported conclusions or proposals. Do not overwrite the draft with published parameters. A refreshed handoff is optional."}
        return {**context, "status": "matched",
                "freshness": freshness,
                "next_step": "Check the supplied evidence for relevance and sufficiency. Reuse supported findings and current target context; analyze or call readonly Look tools for missing or conflicting facts. A matching snapshot does not prove the cause or solution. Form a supported authorized patch before normal compilation and human approval."}


def unavailable_handoff(error):
    return {"blocked": True, "session_created": False, "plan_created": False, "write_executed": False,
            "handoff_status": "unavailable", "error_code": getattr(error, "code", "HANDOFF_UNAVAILABLE"),
            "error": str(error)[:500],
            "next_step": "Use ordinary targeted inspection and begin_edit_session without handoff_ref; no restart is required."}
