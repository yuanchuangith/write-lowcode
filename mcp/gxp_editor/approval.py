from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import EditorConfig, approval_secret
from .hashing import canonical_json, sha256_json


class ApprovalError(RuntimeError):
    pass


def statement_hash(statement: dict[str, Any]) -> str:
    return sha256_json(
        {
            "id": statement["id"],
            "sql": statement["sql"],
            "params": statement["params"],
            "expected_rows": statement["expected_rows"],
            "design_id": statement["design_id"],
            "old_hash": statement["old_hash"],
            "new_hash": statement["new_hash"],
        }
    )


def transaction_binding_hash(artifact: dict[str, Any]) -> str:
    return sha256_json(
        {
            "transaction_id": artifact.get("transaction_id"),
            "session_id": artifact.get("session_id"),
            "plan_id": artifact.get("plan_id"),
            "kind": artifact.get("kind"),
            "design_id": artifact.get("design_id"),
            "ref_id": artifact.get("ref_id"),
            "action_code": artifact.get("action_code"),
            "old_content_hash": artifact.get("old_content_hash"),
            "new_content_hash": artifact.get("new_content_hash"),
            "baseline_last_modification_time": artifact.get("baseline_last_modification_time"),
            "desired_last_modification_time": artifact.get("desired_last_modification_time"),
            "desired_design": artifact.get("desired_design"),
            "expected_relation_rows": artifact.get("expected_relation_rows"),
            "expected_relation_hash": artifact.get("expected_relation_hash"),
            "database_safety": artifact.get("database_safety"),
            "runtime_validation": artifact.get("runtime_validation"),
            "compiler_evidence": artifact.get("compiler_evidence"),
            "statement_hashes": [item.get("statement_hash") for item in artifact.get("statements") or []],
            "created_at": artifact.get("created_at"),
        }
    )


def approval_signature(config: EditorConfig, artifact: dict[str, Any], statement: dict[str, Any], expires_at: str) -> str:
    payload = canonical_json(
        {
            "transaction_id": artifact["transaction_id"],
            "transaction_hash": artifact["transaction_hash"],
            "statement_hash": statement_hash(statement),
            "expires_at": expires_at,
        }
    )
    return hmac.new(approval_secret(config).encode(), payload.encode(), hashlib.sha256).hexdigest()


def approve_statement(config: EditorConfig, artifact: dict[str, Any], statement_id: str) -> dict[str, Any]:
    statement = next((item for item in artifact["statements"] if item["id"] == statement_id), None)
    if not statement:
        raise ApprovalError(f"Statement {statement_id!r} was not found")
    now = datetime.now()
    expires_at = (now + timedelta(minutes=config.approval_ttl_minutes)).isoformat(timespec="seconds")
    approval = {
        "approved_at": now.isoformat(timespec="seconds"),
        "expires_at": expires_at,
        "transaction_hash": artifact.get("transaction_hash"),
        "statement_hash": statement_hash(statement),
        "signature": approval_signature(config, artifact, statement, expires_at),
    }
    artifact.setdefault("approvals", {})[statement_id] = approval
    return approval


def verify_approvals(config: EditorConfig, artifact: dict[str, Any]) -> None:
    if artifact.get("transaction_hash") != transaction_binding_hash(artifact):
        raise ApprovalError("Transaction metadata changed after approval planning")
    approvals = artifact.get("approvals") or {}
    now = datetime.now()
    for statement in artifact.get("statements") or []:
        approval = approvals.get(statement["id"])
        if not approval:
            raise ApprovalError(f"Statement {statement['id']} has not been approved")
        if approval.get("statement_hash") != statement_hash(statement):
            raise ApprovalError(f"Statement {statement['id']} changed after approval")
        if approval.get("transaction_hash") != artifact.get("transaction_hash"):
            raise ApprovalError(f"Transaction metadata changed after approval for statement {statement['id']}")
        expires_at = str(approval.get("expires_at") or "")
        if not expires_at or datetime.fromisoformat(expires_at) < now:
            raise ApprovalError(f"Approval expired for statement {statement['id']}")
        expected = approval_signature(config, artifact, statement, expires_at)
        if not hmac.compare_digest(expected, str(approval.get("signature") or "")):
            raise ApprovalError(f"Approval signature is invalid for statement {statement['id']}")


def load_artifact(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_artifact(path: str | Path, artifact: dict[str, Any]) -> None:
    target = Path(path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
