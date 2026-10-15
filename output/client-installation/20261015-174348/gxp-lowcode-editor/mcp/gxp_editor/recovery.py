"""Read database state and reconcile local bookkeeping without writing business data."""
from __future__ import annotations

import json

from .approval import load_artifact
from .config import load_config, runtime_root
from .db import EditorDatabase
from .execution_lock import execution_lock
from .hashing import relation_snapshot_hash
from .repository import EditorRepository
from .state import StateStore, StateError
from .transaction import validate_transaction_artifact


def archive_edit_session(session_id: str, *, state=None, conversation_id: str = "", owner_revision: int | None = None, expected_snapshot: dict | None = None) -> dict:
    """Called only from the human window after explicit keep-current-data confirmation."""
    from contextlib import ExitStack
    from .execution_lock import approval_window_lock
    from .process_check import assert_approval_process_stopped
    state = state or StateStore()
    session = state.get_session(session_id, include_baseline=False)
    state._assert_owner(session, conversation_id, owner_revision)
    if expected_snapshot is not None and any(session.get(key) != expected_snapshot.get(key) for key in ("state", "transaction_id")):
        raise StateError("会话状态或审批流程已变化，请重新确认释放。")
    transactions = state.session_transactions(session_id)
    current = next((row for row in transactions if row["id"] == session.get("transaction_id")), None)
    with ExitStack() as locks:
        guarded = [row for row in transactions if row["id"] == session.get("transaction_id") or row["state"] in {"awaiting_approval", "executing", "failed", "saved_state_pending"}]
        if session.get("transaction_id") and current is None:
            guarded.append({"id": session["transaction_id"], "artifact_path": str(runtime_root() / "transactions" / f"{session['transaction_id']}.json")})
        for transaction in guarded:
            locks.enter_context(approval_window_lock(transaction["artifact_path"]))
        for transaction in guarded:
            locks.enter_context(execution_lock(transaction["artifact_path"]))
            assert_approval_process_stopped(transaction["id"])
        state.archive_session(
            session_id, conversation_id=conversation_id, owner_revision=session["owner_revision"],
            expected_session_state=session["state"], expected_transaction_id=session.get("transaction_id"),
            expected_transaction_state=current["state"] if current else None,
        )
    return {"session_id": session_id, "outcome": "closed_unverified", "verified": False,
            "session_released": True, "business_data_written": False}


def recover_session(session_id: str, *, state=None, close_changed: bool = False, conversation_id: str | None = "", owner_revision: int | None = None) -> dict:
    state = state or StateStore()
    session = state.get_session(session_id)
    state._assert_owner(session, conversation_id, owner_revision)
    transaction = state.get_transaction(session["transaction_id"])
    with execution_lock(transaction["artifact_path"]):
        transaction = state.get_transaction(transaction["id"])
        if transaction["state"] not in {"executing", "failed", "saved", "saved_state_pending"}:
            raise StateError("此事务无需执行结果恢复，请取消未执行审批或重新生成计划。")
        artifact = load_artifact(transaction["artifact_path"])
        validate_transaction_artifact(artifact)
        if artifact["session_id"] != session_id or artifact["design_id"] != session["design_id"]:
            raise StateError("审批内容与会话身份不匹配。")
        if transaction["state"] == "executing" and artifact.get("execution_lock_version") != 1:
            raise StateError("旧版执行记录未使用执行锁。请关闭旧审批程序，再在处理窗口选择保留数据并释放；不会将未核对结果标记为成功。")
        repository = EditorRepository(EditorDatabase(load_config().database))
        # Lock only for reading: wait for any prior server transaction to settle,
        # and inspect design/relations in the same transaction. No business SQL
        # is executed and the context releases the row lock immediately.
        with repository.database.write_transaction() as database_session:
            current = repository.load_draft_by_id(session["design_id"], for_update=True, session=database_session)
            relations = repository._relations(session["design_id"], database_session)
            relation_hash = relation_snapshot_hash(relations)
            matches_candidate = (current["content_hash"] == artifact["new_content_hash"]
                                 and relation_hash == artifact["expected_relation_hash"])
            old_relation_hash = artifact.get("old_relation_hash")
            if not old_relation_hash and transaction["kind"] == "write":
                baseline = session["baseline"]
                if baseline.get("content_hash") == artifact["old_content_hash"] and "relations" in baseline:
                    old_relation_hash = relation_snapshot_hash(baseline["relations"])
            matches_original = (transaction["state"] not in {"saved", "saved_state_pending"}
                                and current["content_hash"] == artifact["old_content_hash"]
                                and bool(old_relation_hash) and relation_hash == old_relation_hash)
            result = {"session_id": session_id, "transaction_id": transaction["id"],
                      "content_hash": current["content_hash"], "relation_hash": relation_hash,
                      "business_data_written": False, "verified": matches_candidate}
            outcome = "saved" if matches_candidate else "not_applied" if matches_original else "changed"
            if outcome == "changed" and not close_changed:
                return {**result, "outcome": outcome, "session_released": False,
                        "message": "当前数据与审批前后均不一致。可在处理窗口明确选择保留当前数据并结束会话；不会恢复或覆盖数据库。"}
            if outcome == "changed":
                outcome = "closed_changed"
            state.reconcile_transaction(transaction["id"], transaction["state"], outcome,
                                        json.dumps(result, ensure_ascii=False), conversation_id=conversation_id, owner_revision=owner_revision)
        return {**result, "outcome": outcome, "session_released": outcome != "saved"}
