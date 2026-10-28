"""Local identity checks for isolating a pending review, never cancelling it."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .approval import load_artifact
from .state import StateError


def candidate_fingerprint(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def pending_candidate(state, transaction_id, conversation_id, *, queue_id=None,
                      owner_revision=None, artifact_path=None):
    """Caller must hold the artifact execution lock through use of this proof.

    An unreadable artifact can be isolated because the writer atomically claims
    SQLite state before any SQL. Conflicting state/artifact identities or an
    execution marker are never evidence of an unexecuted review.
    """
    transaction = state.get_transaction(transaction_id)
    session = state.get_session(transaction["session_id"], include_baseline=False)
    expected_state = "restore_pending" if transaction.get("kind") == "restore" else "awaiting_approval"
    if (transaction.get("id") != transaction_id or transaction.get("state") != "awaiting_approval"
            or session.get("id") != transaction.get("session_id")
            or session.get("transaction_id") != transaction_id or session.get("state") != expected_state
            or session.get("conversation_id", "") != conversation_id
            or owner_revision is not None and session.get("owner_revision", 0) != owner_revision
            or queue_id is not None and session.get("queue_id") != queue_id
            or artifact_path is not None and Path(transaction["artifact_path"]).resolve() != Path(artifact_path).resolve()):
        raise StateError("审批身份或执行状态未核实，不能作为未执行候选隔离。")
    try:
        artifact = load_artifact(transaction["artifact_path"])
    except (OSError, ValueError):
        artifact = None
    if isinstance(artifact, dict):
        if artifact.get("execution_started") or artifact.get("executed"):
            raise StateError("审批文件已有执行标记，需要先核对结果。")
        for key, expected in (("transaction_id", transaction_id), ("session_id", session["id"]),
                              ("design_id", session["design_id"])):
            if key in artifact and artifact[key] != expected:
                raise StateError("审批文件与本地流程身份不一致，不能隔离或重试执行。")
    return transaction, session, artifact
