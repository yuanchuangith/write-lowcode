from __future__ import annotations

from datetime import datetime
from typing import Any

from .approval import load_artifact, save_artifact, verify_approvals
from .action_context import publication_target
from .config import load_config
from .compiler import CompilerBridge, CompilerError
from .db import EditorDatabase
from .hashing import relation_snapshot_hash
from .handoff_store import environment_scope
from .repository import EditorRepository
from .state import StateStore, assert_session_database_scope
from .transaction import TransactionPlanError, principal_verification, validate_transaction_artifact
from .execution_lock import execution_lock
from .execution_evidence import draft_target_state
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
    edit_session = state.get_session(transaction["session_id"], include_baseline=False)
    assert_session_database_scope(edit_session, config.database)
    artifact = load_artifact(transaction["artifact_path"])
    if transaction["state"] != "awaiting_approval":
        raise WriteExecutionError(f"Transaction state {transaction['state']!r} cannot be executed")
    if artifact.get("execution_started"):
        raise WriteExecutionError("This approval transaction has already been attempted")
    if artifact.get("executed"):
        raise WriteExecutionError("This approval transaction has already been executed")
    validate_transaction_artifact(artifact)
    approved_scope = artifact["database_safety"].get("database_scope")
    if approved_scope is not None and approved_scope != environment_scope(config.database):
        raise WriteExecutionError("Database connection scope changed after approval planning")
    try:
        require_write_business_review(artifact)
    except LogicReviewError as exc:
        raise WriteExecutionError(str(exc)) from exc
    verify_approvals(config, artifact)
    if artifact["kind"] == "write":
        # Reopen preflight is only a point-in-time observation. Recheck the
        # actual frontend/adapter/rules/Node provenance immediately before the
        # execution claim, without regenerating or changing the signed plan.
        expected_compiler = artifact["compiler_evidence"]["compiler_binding"]
        try:
            actual_compiler = CompilerBridge(config).verify_binding(expected_compiler)
        except (CompilerError, ValueError) as exc:
            error = WriteExecutionError("Compiler input source changed after approval planning; create a new plan")
            error.code = "compiler_binding_changed"
            raise error from exc
        if actual_compiler != expected_compiler:
            error = WriteExecutionError("Compiler binding changed after approval planning; create a new plan")
            error.code = "compiler_binding_changed"
            error.details = {"transaction_id": transaction_id, "session_id": artifact["session_id"],
                             "expected_binding_sha256": expected_compiler.get("compiler_binding_sha256"),
                             "current_binding_sha256": actual_compiler.get("compiler_binding_sha256"),
                             "execution_claimed": False,
                             "next_action": "inspect_session_cancellation", "replan_required": True}
            raise error

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
        try:
            principal_status = principal_verification(approved_database_safety, grants.get("authenticated_principal"))
        except TransactionPlanError as exc:
            raise WriteExecutionError(str(exc)) from exc
    except Exception as exc:
        state.release_failed_transaction(transaction_id, "failed_no_write", str(exc))
        raise

    database_saved = False
    write_attempted = False
    session = None
    try:
        with database.write_transaction() as session:
            if "authenticated_principal" in approved_database_safety:
                # The grant read and the eventual write use separate database
                # connections. Check the actual writer connection as well.
                try:
                    principal_verification(approved_database_safety, repository.authenticated_principal(session))
                except TransactionPlanError as exc:
                    raise WriteExecutionError(str(exc)) from exc
            repository.check_statement_targets(artifact["statements"], session)
            current = repository.load_draft_by_id(artifact["design_id"], for_update=True, session=session)
            if draft_target_state(current, artifact["design_id"], artifact["ref_id"]) != "active_unpublished":
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
            if draft_target_state(saved, artifact["design_id"], artifact["ref_id"]) != "active_unpublished":
                raise WriteExecutionError("Saved target is no longer an active unpublished draft before commit")
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
    result = {
        "state": "saved",
        "verified": False,
        "session_released": False,
        "runtime_finalized": False,
        "runtime_mode": "existing_backend_lazy_compile_after_manual_publish",
        "manual_publication_required": True,
        "publication_targets": [],
        "principal_verification": principal_status,
        "executed": True,
        "execution_outcome": "confirmed",
        "commit_evidence": "executor_commit_acknowledged",
        "original_transaction_retry_allowed": False,
    }
    # Verify before the success notice sends the user back to the platform.
    # A pre-commit read alone cannot prove the committed result; uncertain
    # commits never reach this branch. Keep the executor lock through closure.
    try:
        with database.write_transaction() as verification_session:
            committed = repository.load_draft_by_id(
                artifact["design_id"], for_update=True, session=verification_session,
            )
            if draft_target_state(committed, artifact["design_id"], artifact["ref_id"]) != "active_unpublished":
                raise WriteExecutionError("Committed target is no longer an active unpublished draft")
            committed_relations = repository._relations(
                artifact["design_id"], verification_session, for_update=True,
            )
            committed_relation_hash = relation_snapshot_hash(committed_relations)
            if committed["content_hash"] != artifact["new_content_hash"]:
                raise WriteExecutionError("Committed draft hash does not match the approved transaction")
            if committed_relation_hash != artifact["expected_relation_hash"]:
                raise WriteExecutionError("Committed relation snapshot does not match the approved transaction")
        receipt = state.finish_verified_save(
            transaction_id, content_hash=committed["content_hash"], relation_hash=committed_relation_hash,
            conversation_id=edit_session["conversation_id"], owner_revision=edit_session["owner_revision"],
            commit_confirmed=True,
        )
    except Exception as exc:
        # This save has committed. Retain it for recovery without asserting
        # successful verification, retrying the write or rolling it back.
        result["verification_error"] = str(exc)
        return result
    return {**result, "verified": True, "session_released": True, "saved_verification": receipt,
            "publication_targets": [publication_target(artifact.get("action_context"), artifact)]}
