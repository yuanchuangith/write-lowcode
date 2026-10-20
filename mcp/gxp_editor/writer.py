from __future__ import annotations

from datetime import datetime
from typing import Any

from .approval import load_artifact, save_artifact, verify_approvals
from .config import load_config
from .db import EditorDatabase
from .hashing import relation_snapshot_hash
from .repository import EditorRepository, _flag
from .state import StateStore
from .transaction import validate_transaction_artifact
from .execution_lock import execution_lock
from .logic_review import LogicReviewError, require_write_business_review


class WriteExecutionError(RuntimeError):
    pass


def execute_approved_transaction(transaction_id: str) -> dict[str, Any]:
    transaction = StateStore().get_transaction(transaction_id)
    with execution_lock(transaction["artifact_path"]):
        return _execute_approved_transaction(transaction_id)


def _execute_approved_transaction(transaction_id: str) -> dict[str, Any]:
    config = load_config()
    state = StateStore()
    transaction = state.get_transaction(transaction_id)
    artifact = load_artifact(transaction["artifact_path"])
    if transaction["state"] != "awaiting_approval":
        raise WriteExecutionError(f"Transaction state {transaction['state']!r} cannot be executed")
    if artifact.get("execution_started"):
        raise WriteExecutionError("This approval transaction has already been attempted")
    if artifact.get("executed"):
        raise WriteExecutionError("This approval transaction has already been executed")
    validate_transaction_artifact(artifact)
    try:
        require_write_business_review(artifact)
    except LogicReviewError as exc:
        raise WriteExecutionError(str(exc)) from exc
    verify_approvals(config, artifact)

    # Persist protocol identity before claiming execution, so a process crash
    # after the claim can be recovered under the same OS lock.
    artifact["execution_lock_version"] = 1
    save_artifact(transaction["artifact_path"], artifact)
    state.claim_transaction(transaction_id)
    try:
        artifact["execution_started"] = True
        artifact["execution_started_at"] = datetime.now().isoformat(timespec="seconds")
        save_artifact(transaction["artifact_path"], artifact)
        database = EditorDatabase(config.database)
        repository = EditorRepository(database)
        grants = repository.grant_status(
            account_mode=config.database.account_mode,
            expected_grants_sha256=config.database.expected_grants_sha256,
        )
        approved_database_safety = artifact["database_safety"]
        if (
            not grants["safe"]
            or grants["account_mode"] != approved_database_safety["account_mode"]
            or grants["grants_sha256"] != approved_database_safety["grants_sha256"]
        ):
            raise WriteExecutionError(
                "Database account mode or grants changed after approval planning"
            )
    except Exception as exc:
        state.release_failed_transaction(transaction_id, "failed_no_write", str(exc))
        raise

    database_saved = False
    write_attempted = False
    session = None
    try:
        with database.write_transaction() as session:
            repository.check_statement_targets(artifact["statements"], session)
            current = repository.load_draft_by_id(artifact["design_id"], for_update=True, session=session)
            if _flag(current.get("is_publish")) or _flag(current.get("isDeleted")):
                raise WriteExecutionError("The locked row is no longer an active unpublished draft")
            if current["content_hash"] != artifact["old_content_hash"]:
                raise WriteExecutionError("The draft content changed after planning; transaction was not executed")
            if str(current["last_modification_time"]) != artifact["baseline_last_modification_time"]:
                raise WriteExecutionError("The draft modification timestamp changed after planning")
            current_relations = repository._relations(artifact["design_id"], session, for_update=True)
            if relation_snapshot_hash(current_relations) != artifact.get("old_relation_hash"):
                raise WriteExecutionError("The draft relations changed after planning; transaction was not executed")
            for statement in artifact["statements"]:
                write_attempted = True
                affected = session.execute(statement["sql"], statement["params"])
                if affected != int(statement["expected_rows"]):
                    raise WriteExecutionError(
                        f"Statement {statement['id']} affected {affected} rows; expected {statement['expected_rows']}"
                    )
            saved = repository.load_draft_by_id(artifact["design_id"], session=session)
            if saved["content_hash"] != artifact["new_content_hash"]:
                raise WriteExecutionError("Saved draft hash differs before commit")
            relations = repository._relations(artifact["design_id"], session)
            if relation_snapshot_hash(relations) != artifact["expected_relation_hash"]:
                raise WriteExecutionError("Saved relation snapshot differs before commit")
        database_saved = True
        artifact["executed"] = True
        artifact["executed_statement_hashes"] = [item["statement_hash"] for item in artifact["statements"]]
        save_artifact(transaction["artifact_path"], artifact)
        state.update_transaction(transaction_id, "saved")
        state.update_session(transaction["session_id"], "saved", conversation_id=None)
    except Exception as exc:
        if database_saved:
            state.update_transaction(
                transaction_id,
                "saved_state_pending",
                f"Database committed but local state update failed: {exc}",
            )
            state.update_session(transaction["session_id"], "saved_state_pending", conversation_id=None)
        else:
            if not write_attempted:
                state.release_failed_transaction(transaction_id, "failed_no_write", str(exc))
            elif getattr(session, "rollback_confirmed", False):
                state.release_failed_transaction(transaction_id, "rolled_back", str(exc))
            else:
                state.update_transaction(transaction_id, "failed", str(exc))
        raise
    return {
        "state": "saved",
        "runtime_finalized": False,
        "runtime_mode": "existing_backend_lazy_compile_after_manual_publish",
        "manual_publication_required": True,
    }
