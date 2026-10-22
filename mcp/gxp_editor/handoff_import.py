"""Validate readonly evidence before creating any local editing occupancy."""
from __future__ import annotations

import json

from .handoff_store import HandoffError, HandoffStore, environment_scope, snapshot_targets, compare_targets
from .hashing import sha256_text
from .telemetry import stage


def inspect_handoff(reference, draft, database_config):
    with stage("handoff.verify"):
        if not isinstance(reference, dict) or set(reference) != {"handoff_id", "sha256"}:
            raise HandoffError("HANDOFF_REF", "handoff_ref requires exactly handoff_id and sha256")
        record = HandoffStore().read(reference["handoff_id"], reference["sha256"])
        if record["environment_scope"] != environment_scope(database_config):
            raise HandoffError("HANDOFF_ENVIRONMENT", "Handoff belongs to a different configured environment")
        if record["action"]["ref_id"] != str(draft["ref_id"]) or record["draft"]["design_id"] != str(draft["id"]):
            raise HandoffError("HANDOFF_TARGET", "Handoff does not identify the selected active draft")
        if record["action"].get("action_code") and record["action"]["action_code"] != str(draft.get("code") or ""):
            raise HandoffError("HANDOFF_TARGET", "Handoff action code does not match the selected draft")
        data = draft.get("data_json")
        if data is None:
            data = json.loads(draft["data"])
        targets = [{"group_key": item["group_key"], "node_key": item["node_key"]} for item in record["targets"]]
        # Keep the original requirements and diagnosis available even when a
        # targeted refresh is needed. A stale record is context, not current
        # proof or permission to apply its proposed values.
        context = {"handoff_ref": reference, "targets": targets,
                   "diagnosis_source": record["diagnosis_source"], "change_spec": record["change_spec"],
                   "authorization_inherited": False, "approval_inherited": False}
        current = snapshot_targets(data, targets, allow_missing=True)
        differences = compare_targets(record["targets"], current)
        if sha256_text(str(draft.get("data") or "")) != record["draft"]["data_sha256"] or differences:
            return {**context, "status": "stale", "target_differences": differences,
                    "draft_content_changed": True,
                    "next_step": "Assess the saved evidence against changed targets and dependencies. Analyze or call readonly Look tools for missing facts, then optionally refresh the record or begin ordinary inspection without handoff_ref. No session was created."}
        if record["source_target_differences"]:
            return {**context, "status": "source_target_diverged",
                    "target_differences": record["source_target_differences"],
                    "next_step": "Assess the differing draft locations and dependencies using the saved evidence as context. Analyze or call readonly Look tools as needed; revise unsupported conclusions or proposals. Do not overwrite the draft with published parameters. A refreshed handoff is optional."}
        return {**context, "status": "matched",
                "next_step": "Check the supplied evidence for relevance and sufficiency. Reuse supported findings and current target context; analyze or call readonly Look tools for missing or conflicting facts. A matching snapshot does not prove the cause or solution. Form a supported authorized patch before normal compilation and human approval."}


def unavailable_handoff(error):
    return {"blocked": True, "session_created": False, "plan_created": False, "write_executed": False,
            "handoff_status": "unavailable", "error_code": getattr(error, "code", "HANDOFF_UNAVAILABLE"),
            "error": str(error)[:500],
            "next_step": "Use ordinary targeted inspection and begin_edit_session without handoff_ref; no restart is required."}
