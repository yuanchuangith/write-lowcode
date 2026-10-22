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
        current = snapshot_targets(data, targets, allow_missing=True)
        differences = compare_targets(record["targets"], current)
        if sha256_text(str(draft.get("data") or "")) != record["draft"]["data_sha256"] or differences:
            return {"status": "stale", "handoff_ref": reference, "target_differences": differences,
                    "draft_content_changed": True, "targets": targets,
                    "next_step": "Inspect only changed targets and their dependencies; export a refreshed handoff or begin without handoff_ref. No session was created."}
        if record["source_target_differences"]:
            return {"status": "source_target_diverged", "handoff_ref": reference,
                    "target_differences": record["source_target_differences"], "targets": targets,
                    "next_step": "Review the differing draft locations against the diagnosed copy and update the proposed change; do not overwrite the draft with published parameters."}
        return {"status": "matched", "handoff_ref": reference, "targets": targets,
                "diagnosis_source": record["diagnosis_source"], "change_spec": record["change_spec"],
                "authorization_inherited": False, "approval_inherited": False,
                "next_step": "Apply only the confirmed change scope using current draft operation templates; normal compilation and human approval remain required."}


def unavailable_handoff(error):
    return {"blocked": True, "session_created": False, "plan_created": False, "write_executed": False,
            "handoff_status": "unavailable", "error_code": getattr(error, "code", "HANDOFF_UNAVAILABLE"),
            "error": str(error)[:500],
            "next_step": "Use ordinary targeted inspection and begin_edit_session without handoff_ref; no restart is required."}
