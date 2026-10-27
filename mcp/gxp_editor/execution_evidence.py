"""Separate observed database content from evidence of this executor's commit."""
from __future__ import annotations

import json
from typing import Any


def draft_target_state(row: Any, design_id: str, ref_id: str) -> str:
    """Content hashes do not cover draft identity/publication/deletion gates."""
    if not isinstance(row, dict) or any(key not in row for key in ("id", "ref_id", "is_publish", "isDeleted")):
        return "unknown"
    if str(row["id"]) != design_id or str(row["ref_id"]) != ref_id:
        return "changed"
    for key in ("is_publish", "isDeleted"):
        if row[key] in (True, 1, "1", b"\x01"):
            return "changed"
        if row[key] not in (False, 0, "0", b"\x00"):
            return "unknown"
    return "active_unpublished"


def _observation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    if value.get("execution_outcome") not in {"confirmed", "unknown", "not_started"}:
        return None
    if value.get("execution_outcome") == "confirmed" and value.get("commit_evidence") not in {
        "executor_commit_receipt", "executor_commit_acknowledged", "historical_executor_receipt",
    }:
        return None
    return dict(value)


def execution_evidence(transaction: dict[str, Any], artifact=None, observation=None) -> dict[str, Any]:
    artifact = artifact if isinstance(artifact, dict) else {}
    persisted = _observation(observation)
    if persisted is not None:
        result = persisted
    else:
        try:
            message = json.loads(transaction.get("message") or "{}")
        except (ValueError, TypeError):
            message = {}
        saved = message.get("saved_verification") if isinstance(message, dict) else None
        hashes = [item.get("statement_hash") for item in artifact.get("statements", []) if isinstance(item, dict)]
        acknowledged = (artifact.get("executed") is True and bool(hashes) and all(hashes)
                        and artifact.get("executed_statement_hashes") == hashes)
        historical = _observation(saved)
        pending = (transaction.get("state") == "awaiting_approval"
                   and not artifact.get("execution_started") and not artifact.get("executed"))
        result = historical or {"schema_version": 1,
                  "execution_outcome": "confirmed" if acknowledged or historical else "not_started" if pending else "unknown",
                  "commit_evidence": "executor_commit_receipt" if acknowledged else "historical_executor_receipt" if historical else "unavailable",
                  "verification_scope": "not_rechecked"}
    result.setdefault("current_matches_baseline", None)
    result.setdefault("current_matches_candidate", None)
    result["executed"] = result.get("execution_outcome") == "confirmed"
    result["original_transaction_retry_allowed"] = False
    return result
