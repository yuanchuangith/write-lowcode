"""Read-only comparison of bound draft evidence with one current snapshot.

The caller is responsible for validating artifacts and reading the current
draft and complete relations in one database transaction.  Diagnostic hashes
explain differences; they never replace the existing content/relation hashes
or authorize a write, approval, retry, or baseline refresh.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .hashing import RELATION_HASH_FIELDS, design_content_hash, relation_snapshot_hash, sha256_json


COMPARISON_SCHEMA_VERSION = 1
DIAGNOSTIC_SCHEMA_VERSION = 1
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_BODY_FIELDS = ("data", "js_code", "csharp_code")
_CONTENT_FIELDS = ("id", "ref_id", *_BODY_FIELDS, "last_modification_time")


class BaselineComparisonError(RuntimeError):
    """A service-level evidence conflict, independent of compiler errors."""

    def __init__(self, code: str, message: str, details: dict[str, Any]):
        super().__init__(message)
        self.code = code
        self.details = details


def _digest(value: Any) -> str | None:
    return value if isinstance(value, str) and _DIGEST.fullmatch(value) else None


def _identity(row: dict[str, Any] | None) -> dict[str, str]:
    row = row or {}
    return {"design_id": str(row.get("design_id") or row.get("id") or ""),
            "ref_id": str(row.get("ref_id") or "")}


def _flag(value: Any) -> bool | None:
    if value in (False, 0, "0", b"\x00"):
        return False
    if value in (True, 1, "1", b"\x01"):
        return True
    return None


def draft_diagnostics(row: dict[str, Any]) -> dict[str, Any]:
    """Versioned, presence-aware hashes. Never return design/code bodies."""
    identity = {key: {"present": key in row, "value": row.get(key)} for key in ("id", "ref_id")}
    payload = {key: {"present": key in row, "value": row.get(key)} for key in _BODY_FIELDS}
    return {
        "diagnostic_schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "identity": _identity(row),
        "payload_sha256": sha256_json({"schema_version": DIAGNOSTIC_SCHEMA_VERSION,
                                       "identity": identity, "fields": payload}),
        "fields": {key: {"present": key in row, "sha256": sha256_json(value)}
                   for key, value in payload.items()},
        "revision_sha256": sha256_json({"present": "last_modification_time" in row,
                                        "value": row.get("last_modification_time")}),
    }


def _complete_design(row: Any) -> bool:
    return (isinstance(row, dict) and all(field in row for field in _CONTENT_FIELDS)
            and bool(row["id"]) and bool(row["ref_id"]))


def _relation_rows(rows: Any, design_id: str) -> bool:
    if not isinstance(rows, list):
        return False
    identifiers = set()
    for row in rows:
        if not isinstance(row, dict) or any(field not in row for field in RELATION_HASH_FIELDS):
            return False
        identifier = str(row.get("id") or "")
        if not identifier or identifier in identifiers or str(row.get("design_id") or "") != design_id:
            return False
        identifiers.add(identifier)
    return True


def _relation_differences(expected: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, Any]:
    before = {str(row["id"]): relation_snapshot_hash([row]) for row in expected}
    after = {str(row["id"]): relation_snapshot_hash([row]) for row in current}
    changed = [{"relation_id": key, "change": "added" if key not in before else "removed" if key not in after else "modified",
                "expected_sha256": before.get(key), "current_sha256": after.get(key)}
               for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]
    return {"changed_count": len(changed), "items": changed[:32], "truncated": len(changed) > 32}


def compare_baseline(
    baseline: dict[str, Any] | None = None,
    current: dict[str, Any] | None = None,
    current_relations: list[dict[str, Any]] | None = None,
    *,
    artifact: dict[str, Any] | None = None,
    expected_identity: dict[str, Any] | None = None,
    relations_complete: bool = True,
    read_error: str | None = None,
    session_id: str = "",
    backup_id: str = "",
    include_relation_differences: bool = False,
) -> dict[str, Any]:
    """Compare current evidence; missing required evidence yields ``unknown``.

    A previously validated artifact has priority over session baseline hashes.
    Its old relation binding may only fall back to a baseline whose content
    hash matches that artifact. Candidate relation hashes are never old hashes.
    Corrupt or unavailable diagnostic bodies do not block matching core hashes.
    ``read_error`` must be a fixed reason code, never an exception/data string.
    """
    baseline = baseline if isinstance(baseline, dict) else {}
    artifact = artifact if isinstance(artifact, dict) else {}
    expected = _identity(expected_identity or artifact or baseline)
    old_content = _digest(artifact.get("old_content_hash")) if artifact else None
    if not artifact:
        old_content = _digest(baseline.get("content_hash"))
    old_relations = _digest(artifact.get("old_relation_hash"))
    baseline_usable = (_complete_design(baseline) and _digest(baseline.get("content_hash")) == old_content
                       and design_content_hash(baseline) == old_content)
    baseline_relations = baseline.get("relations")
    if (old_relations is None and baseline_usable
            and _relation_rows(baseline_relations, expected["design_id"])):
        old_relations = relation_snapshot_hash(baseline_relations)

    result: dict[str, Any] = {
        "comparison_schema_version": COMPARISON_SCHEMA_VERSION,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "status": "unknown", "identity": expected, "session_id": session_id,
        "backup_id": backup_id, "change_categories": [], "evidence_gaps": [],
        "expected": {"content_hash": old_content, "relation_sha256": old_relations},
        "current": {"content_hash": None, "relation_sha256": None},
        "diagnostics": {"available": False}, "read_only": True, "write_executed": False,
        "minimum_next_step": {"tool": "find_edit_nodes", "arguments": {"session_id": session_id},
                              "instruction": "Restore the unavailable read evidence and compare again. Do not refresh the old baseline, approve, execute, or infer that no change occurred."},
    }
    gaps = result["evidence_gaps"]
    categories = result["change_categories"]
    if not old_content:
        gaps.append("baseline_content_hash_unavailable")
    if not old_relations:
        gaps.append("baseline_relations_unavailable")
    if not all(expected.values()):
        gaps.append("baseline_identity_unavailable")
    if read_error:
        # Restrict observations to fixed reason codes so exception text cannot
        # accidentally disclose a SQL argument, business payload or credential.
        safe_reason = read_error if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", read_error) else "current_snapshot_unavailable"
        gaps.append(safe_reason)
    if not _complete_design(current):
        gaps.append("current_draft_unavailable")
        return result
    current_identity = _identity(current)
    result["current"]["identity"] = current_identity
    result["current"]["content_hash"] = design_content_hash(current)
    if current_identity != expected:
        categories.append("identity_changed")
    for key, category in (("is_publish", "publication_state_changed"), ("isDeleted", "deletion_state_changed")):
        state = _flag(current.get(key))
        if key not in current or state is None:
            gaps.append(f"current_{key}_unverified")
        elif state:
            categories.append(category)

    if not relations_complete or not _relation_rows(current_relations, current_identity["design_id"]):
        gaps.append("current_relations_incomplete")
    else:
        result["current"]["relation_sha256"] = relation_snapshot_hash(current_relations)
    if old_content and result["current"]["content_hash"] != old_content:
        categories.append("draft_content_changed")
    if old_relations and result["current"]["relation_sha256"] and result["current"]["relation_sha256"] != old_relations:
        categories.append("draft_relations_changed")

    if baseline_usable:
        before, after = draft_diagnostics(baseline), draft_diagnostics(current)
        changed_fields = {key: before["fields"][key] != after["fields"][key] for key in _BODY_FIELDS}
        result["diagnostics"] = {"available": True, "expected": before, "current": after,
                                 "changed_fields": changed_fields,
                                 "payload_changed": before["payload_sha256"] != after["payload_sha256"]}
        if "draft_content_changed" in categories and not result["diagnostics"]["payload_changed"]:
            categories[categories.index("draft_content_changed")] = "revision_only_changed"
        if (include_relation_differences and _relation_rows(baseline_relations, expected["design_id"])
                and result["current"]["relation_sha256"]):
            result["diagnostics"]["relations"] = _relation_differences(baseline_relations, current_relations)
    # All required evidence must be available before declaring a match/stale.
    # Even a known mismatch does not make an incomplete snapshot verified.
    result["status"] = "unknown" if gaps else "stale" if categories else "match"
    if result["status"] == "match":
        result["minimum_next_step"] = {"instruction": "Continue the existing guarded workflow; this comparison does not grant approval or authorize execution."}
    elif result["status"] == "stale":
        result["minimum_next_step"] = {"tool": "inspect_session_cancellation", "arguments": {"session_id": session_id},
                                      "instruction": "Inspect cancellation scope, obtain explicit confirmation, cancel by fingerprint, then create a new session from the current draft."}
    return result


def require_baseline_match(comparison: dict[str, Any]) -> None:
    if comparison.get("status") == "match":
        return
    categories = comparison.get("change_categories") or []
    unknown = comparison.get("status") != "stale"
    code = ("baseline_evidence_unavailable" if unknown else "draft_relations_changed"
            if categories == ["draft_relations_changed"] else "draft_baseline_changed")
    message = ("Required draft baseline evidence is unavailable; no match has been established" if unknown
               else "Draft relations changed after the edit session began; target evidence is stale"
               if code == "draft_relations_changed"
               else "Draft changed after the edit session began; target evidence is stale")
    raise BaselineComparisonError(code, message, comparison)
