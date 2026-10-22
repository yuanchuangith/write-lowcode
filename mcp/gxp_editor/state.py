from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import runtime_root
from .hashing import sha256_json


class StateError(RuntimeError):
    pass


ACTIVE_STATES = (
    "locked",
    "planned",
    "awaiting_approval",
    "saved",
    "saved_state_pending",
    "verified",
    "restore_pending",
)

_UNSET_PLAN = object()


def saved_verification(session: dict[str, Any], transaction: dict[str, Any]) -> dict[str, Any] | None:
    """Return historical save evidence only for this finished transaction."""
    if (session["state"] != "finished" or transaction["state"] != "saved"
            or session.get("transaction_id") != transaction["id"]
            or transaction["session_id"] != session["id"]):
        return None
    try:
        message = json.loads(transaction.get("message") or "{}")
    except (TypeError, ValueError):
        return None
    receipt = message.get("saved_verification") if isinstance(message, dict) else None
    if (not isinstance(receipt, dict) or receipt.get("scope") != "committed_draft_snapshot"
            or receipt.get("design_id") != session["design_id"]
            or not all(receipt.get(key) for key in ("content_hash", "relation_hash", "verified_at"))):
        return None
    return receipt


class StateStore:
    def __init__(self, path: Path | None = None):
        self.path = path or runtime_root() / "state" / "editor.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.executescript(
                """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  ref_id TEXT NOT NULL,
  design_id TEXT NOT NULL,
  action_code TEXT NOT NULL,
  action_name TEXT NOT NULL,
  state TEXT NOT NULL,
  baseline_json TEXT NOT NULL,
  backup_id TEXT NOT NULL,
  compiler_hash TEXT NOT NULL,
  plan_id TEXT,
  transaction_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  status TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  base_hash TEXT NOT NULL,
  new_hash TEXT NOT NULL,
  compiler_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  plan_id TEXT,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""
            )
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
            if "session_mode" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN session_mode TEXT NOT NULL DEFAULT 'edit'")
            if "conversation_id" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN conversation_id TEXT NOT NULL DEFAULT ''")
            if "owner_revision" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN owner_revision INTEGER NOT NULL DEFAULT 0")
            if "queue_id" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN queue_id TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, label TEXT NOT NULL, created_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS session_closures (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS edit_queues (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, created_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS edit_queue_items (queue_id TEXT NOT NULL, session_id TEXT PRIMARY KEY, owner_revision INTEGER NOT NULL, position INTEGER NOT NULL, context_json TEXT NOT NULL)")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_conversation(self, label: str = "") -> dict[str, str]:
        if not isinstance(label, str) or len(label) > 120:
            raise StateError("对话名称须为不超过 120 个字符的文本。")
        conversation_id = uuid.uuid4().hex
        with self._connection() as db:
            db.execute("INSERT INTO conversations VALUES (?,?,?)", (conversation_id, label.strip(), datetime.now().isoformat(timespec="seconds")))
        return {"conversation_id": conversation_id, "label": label.strip()}

    def _conversation_exists(self, db, conversation_id: str) -> None:
        if not isinstance(conversation_id, str) or len(conversation_id) > 128:
            raise StateError("对话标记无效。")
        if conversation_id and not db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone():
            raise StateError("对话标记不存在，请先调用 create_conversation。")

    def create_edit_queue(self, conversation_id: str) -> str:
        if not conversation_id:
            raise StateError("连续审批需要已注册的 conversation_id。")
        queue_id = uuid.uuid4().hex
        with self._connection() as db:
            self._conversation_exists(db, conversation_id)
            db.execute("INSERT INTO edit_queues VALUES (?,?,?)", (queue_id, conversation_id, datetime.now().isoformat(timespec="seconds")))
        return queue_id

    @staticmethod
    def _assert_queue_owner(db, queue_id: str, conversation_id: str) -> None:
        queue = db.execute("SELECT conversation_id FROM edit_queues WHERE id=?", (queue_id,)).fetchone()
        if not queue or queue["conversation_id"] != conversation_id:
            raise StateError("审批队列不存在或属于其他对话。")

    def edit_queue(self, queue_id: str, conversation_id: str) -> list[dict[str, Any]]:
        with self._connection() as db:
            self._assert_queue_owner(db, queue_id, conversation_id)
            rows = db.execute("""
SELECT s.id,s.ref_id,s.design_id,s.action_code,s.action_name,s.state,s.plan_id,s.transaction_id,
       s.conversation_id,s.owner_revision,s.queue_id,i.owner_revision AS queued_owner_revision,i.position,i.context_json,
       t.state AS transaction_state, t.artifact_path, p.status AS plan_status
FROM edit_queue_items i JOIN sessions s ON s.id=i.session_id
LEFT JOIN transactions t ON t.id=s.transaction_id
LEFT JOIN plans p ON p.id=s.plan_id
WHERE i.queue_id=? ORDER BY i.position
""", (queue_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["action_context"] = json.loads(item.pop("context_json"))
            result.append(item)
        return result

    @staticmethod
    def _assert_owner(session, conversation_id: str | None, owner_revision: int | None = None) -> None:
        if session is None:
            raise StateError("会话不存在。")
        if conversation_id is not None and session["conversation_id"] != conversation_id:
            raise StateError("此流程属于其他对话，当前对话不能操作；请用会话标记显式接手。")
        if owner_revision is not None and session["owner_revision"] != owner_revision:
            raise StateError("会话归属已经变更，请重新读取会话。")

    def active_session(self, conversation_id: str | None = None) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        with self._connection() as db:
            owner_filter = " AND conversation_id=?" if conversation_id is not None else ""
            values = (*ACTIVE_STATES, conversation_id) if conversation_id is not None else ACTIVE_STATES
            return self._row(db.execute(f"SELECT * FROM sessions WHERE state IN ({placeholders}){owner_filter} ORDER BY created_at LIMIT 1", values).fetchone())

    def target_session(self, design_id: str, ref_id: str) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        with self._connection() as db:
            return self._row(db.execute(f"SELECT id,conversation_id,state FROM sessions WHERE state IN ({placeholders}) AND (design_id=? OR ref_id=?) LIMIT 1", (*ACTIVE_STATES, design_id, ref_id)).fetchone())

    def create_session(self, draft: dict[str, Any], backup_id: str, compiler_hash: str, *, session_mode: str = "edit", conversation_id: str = "", queue_id: str = "") -> dict[str, Any]:
        if session_mode not in {"edit", "reference_repair"}:
            raise StateError("Unsupported session mode")
        now = datetime.now().isoformat(timespec="seconds")
        session_id = uuid.uuid4().hex
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._conversation_exists(db, conversation_id)
            if queue_id:
                self._assert_queue_owner(db, queue_id, conversation_id)
            placeholders = ",".join("?" for _ in ACTIVE_STATES)
            active = db.execute(f"SELECT id, ref_id FROM sessions WHERE state IN ({placeholders}) AND ((conversation_id=? AND (?='' OR queue_id!=?)) OR design_id=? OR ref_id=?) LIMIT 1", (*ACTIVE_STATES, conversation_id, queue_id, queue_id, str(draft["id"]), str(draft["ref_id"]))).fetchone()
            if active:
                raise StateError(f"Another edit session is active: {active['id']} ref_Id={active['ref_id']}")
            metadata = draft.get("metadata") or {}
            db.execute(
                """
INSERT INTO sessions
(id,ref_id,design_id,action_code,action_name,state,baseline_json,backup_id,
 compiler_hash,plan_id,transaction_id,created_at,updated_at,session_mode,conversation_id,queue_id)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
""",
                (
                    session_id,
                    str(draft["ref_id"]),
                    str(draft["id"]),
                    str(metadata.get("action_code") or draft.get("code") or ""),
                    str(metadata.get("action_name") or draft.get("name") or ""),
                    "locked",
                    json.dumps(draft, ensure_ascii=False),
                    backup_id,
                    compiler_hash,
                    None,
                    None,
                    now,
                    now,
                    session_mode,
                    conversation_id,
                    queue_id,
                ),
            )
            if queue_id:
                from .action_context import action_context
                position = db.execute("SELECT COALESCE(MAX(position),0)+1 FROM edit_queue_items WHERE queue_id=?", (queue_id,)).fetchone()[0]
                db.execute("INSERT INTO edit_queue_items VALUES (?,?,?,?,?)", (queue_id, session_id, 0, position, json.dumps(action_context(draft), ensure_ascii=False)))
        return self.get_session(session_id)

    def get_session(self, session_id: str, *, include_baseline: bool = True) -> dict[str, Any]:
        with self._connection() as db:
            row = self._row(db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())
        if not row:
            raise StateError(f"Session {session_id!r} was not found")
        baseline_json = row.pop("baseline_json")
        if include_baseline:
            row["baseline"] = json.loads(baseline_json)
        return row

    def update_session(self, session_id: str, state: str, *, conversation_id: str | None = "", owner_revision: int | None = None, expected_states: tuple[str, ...] | None = None, expected_transaction_id: str | None = None, **fields: Any) -> None:
        allowed = {"plan_id", "transaction_id"}
        assignments = ["state=?", "updated_at=?"]
        values: list[Any] = [state, datetime.now().isoformat(timespec="seconds")]
        for key, value in fields.items():
            if key not in allowed:
                raise StateError(f"Unsupported session field: {key}")
            assignments.append(f"{key}=?")
            values.append(value)
        values.append(session_id)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            self._assert_owner(session, conversation_id, owner_revision)
            if expected_states is not None and session["state"] not in expected_states:
                raise StateError("会话状态已变化，请重新核对后操作。")
            if expected_transaction_id is not None and session["transaction_id"] != expected_transaction_id:
                raise StateError("当前审批流程已变化，请重新核对后操作。")
            db.execute(f"UPDATE sessions SET {', '.join(assignments)} WHERE id=?", values)

    def create_plan(self, session_id: str, artifact_path: str, base_hash: str, new_hash: str, compiler_hash: str, *, conversation_id: str | None = "", owner_revision: int | None = None, initial_status: str = "ready", expected_plan_id: Any = _UNSET_PLAN) -> str:
        if initial_status not in {"ready", "blocked"}:
            raise StateError("A new plan must be ready or blocked")
        plan_id = uuid.uuid4().hex
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            self._assert_owner(session, conversation_id, owner_revision)
            if not session or session["state"] not in {"locked", "planned"}:
                raise StateError("Session can no longer be planned")
            if expected_plan_id is not _UNSET_PLAN and session["plan_id"] != expected_plan_id:
                raise StateError("Current plan changed while this candidate was being generated; retry from the current plan")
            db.execute("UPDATE plans SET status='superseded' WHERE session_id=? AND status IN ('ready','blocked')", (session_id,))
            db.execute(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?)",
                (plan_id, session_id, initial_status, artifact_path, base_hash, new_hash, compiler_hash, datetime.now().isoformat(timespec="seconds")),
            )
            db.execute("UPDATE sessions SET state='planned', plan_id=?, updated_at=? WHERE id=?",
                       (plan_id, datetime.now().isoformat(timespec="seconds"), session_id))
        return plan_id

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = self._row(db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone())
        if not row:
            raise StateError(f"Plan {plan_id!r} was not found")
        return row

    def update_plan_status(self, plan_id: str, status: str) -> None:
        with self._connection() as db:
            affected = db.execute("UPDATE plans SET status=? WHERE id=?", (status, plan_id)).rowcount
        if affected != 1:
            raise StateError(f"Plan {plan_id!r} was not found")

    def create_transaction(
        self,
        session_id: str,
        plan_id: str | None,
        kind: str,
        artifact_path: str,
        *,
        transaction_id: str | None = None,
        conversation_id: str | None = "",
        owner_revision: int | None = None,
    ) -> str:
        transaction_id = transaction_id or uuid.uuid4().hex
        now = datetime.now().isoformat(timespec="seconds")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT id FROM transactions WHERE session_id=? AND state IN ('awaiting_approval','executing') LIMIT 1",
                (session_id,),
            ).fetchone()
            if active:
                raise StateError(f"Session already has an active approval transaction: {active['id']}")
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            self._assert_owner(session, conversation_id, owner_revision)
            allowed = {"planned"} if kind == "write" else {"saved", "saved_state_pending", "verified"}
            if not session or session["state"] not in allowed or (kind == "write" and session["plan_id"] != plan_id):
                raise StateError("Session can no longer enter approval")
            db.execute(
                "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?)",
                (transaction_id, session_id, plan_id, kind, "awaiting_approval", artifact_path, "", now, now),
            )
            db.execute("UPDATE sessions SET state=?, transaction_id=?, updated_at=? WHERE id=?",
                       ("awaiting_approval" if kind == "write" else "restore_pending", transaction_id, now, session_id))
            if kind == "write":
                affected = db.execute("UPDATE plans SET status='frozen' WHERE id=? AND session_id=? AND status='ready'", (plan_id, session_id)).rowcount
                if affected != 1:
                    raise StateError("Plan is no longer ready for approval")
        return transaction_id

    def claim_transaction(self, transaction_id: str) -> None:
        """Serialize execution against manual cancellation across processes."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            affected = db.execute(
                "UPDATE transactions SET state='executing', updated_at=? WHERE id=? AND state='awaiting_approval' "
                "AND session_id IN (SELECT id FROM sessions WHERE state IN ('awaiting_approval','restore_pending') AND transaction_id=?)",
                (datetime.now().isoformat(timespec="seconds"), transaction_id, transaction_id),
            ).rowcount
            if affected != 1:
                raise StateError("Transaction is no longer awaiting approval; execution was blocked")

    def finish_verified_save(self, transaction_id: str, *, content_hash: str, relation_hash: str,
                             conversation_id: str, owner_revision: int) -> dict[str, Any]:
        """Atomically retain post-commit verification evidence and release its session."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            transaction = db.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            if not transaction or transaction["state"] != "saved":
                raise StateError("Only a committed save can finish verification")
            session = db.execute("SELECT * FROM sessions WHERE id=?", (transaction["session_id"],)).fetchone()
            self._assert_owner(session, conversation_id, owner_revision)
            if session["transaction_id"] != transaction_id or session["state"] not in {"saved", "verified"}:
                raise StateError("Session changed before save verification could finish")
            receipt = {"scope": "committed_draft_snapshot", "design_id": session["design_id"],
                       "content_hash": content_hash, "relation_hash": relation_hash,
                       "verified_at": datetime.now().isoformat(timespec="milliseconds")}
            message = {"saved_verification": receipt}
            if transaction["message"]:
                message["previous_message"] = transaction["message"]
            db.execute("UPDATE transactions SET message=?,updated_at=? WHERE id=?",
                       (json.dumps(message, ensure_ascii=False), receipt["verified_at"], transaction_id))
            db.execute("UPDATE sessions SET state='finished',updated_at=? WHERE id=?",
                       (receipt["verified_at"], session["id"]))
        return receipt

    @staticmethod
    def _cancellation_snapshot(db, session) -> dict[str, Any]:
        snapshot = {"session": {key: session[key] for key in (
            "id", "action_code", "action_name", "state", "conversation_id", "owner_revision",
            "plan_id", "transaction_id", "backup_id", "updated_at")},
            "plans": [dict(row) for row in db.execute(
                "SELECT * FROM plans WHERE session_id=? ORDER BY id", (session["id"],))],
            "transactions": [dict(row) for row in db.execute(
                "SELECT * FROM transactions WHERE session_id=? ORDER BY id", (session["id"],))]}
        return {**snapshot, "fingerprint": sha256_json(snapshot)}

    def cancellation_snapshot(self, session_id: str, *, conversation_id: str = "") -> dict[str, Any]:
        with self._connection() as db:
            db.execute("BEGIN")
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            self._assert_owner(session, conversation_id, None)
            return self._cancellation_snapshot(db, session)

    @staticmethod
    def _require_chat_cancellable(snapshot: dict[str, Any]) -> None:
        from .approval import load_artifact
        session, transactions = snapshot["session"], snapshot["transactions"]
        if session["state"] not in {"locked", "planned", "awaiting_approval"}:
            raise StateError("聊天取消仅适用于未执行的编辑计划和审批；保存、恢复或结果不明的流程需核验处理。")
        if session["transaction_id"] and not any(row["id"] == session["transaction_id"] for row in transactions):
            raise StateError("当前审批记录缺失，请使用会话处理窗口核对。")
        for transaction in transactions:
            if transaction["kind"] != "write" or transaction["state"] not in {
                "awaiting_approval", "cancelled", "failed_no_write", "rolled_back", "not_applied",
            }:
                raise StateError("执行已经开始或结果尚未确认，不能通过聊天取消。")
            if transaction["state"] == "awaiting_approval":
                try:
                    artifact = load_artifact(transaction["artifact_path"])
                except (OSError, ValueError):
                    continue
                if isinstance(artifact, dict) and (artifact.get("execution_started") or artifact.get("executed")):
                    raise StateError("Execution has already started; the session cannot be aborted")

    def abort_session(self, session_id: str, *, conversation_id: str | None = "", owner_revision: int | None = None,
                      expected_fingerprint: str | None = None, user_confirmation: str = "") -> None:
        """Cancel from the human UI or a snapshot-bound explicit chat confirmation."""
        from .approval import load_artifact

        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            self._assert_owner(session, conversation_id, owner_revision)
            if expected_fingerprint is not None:
                if not isinstance(user_confirmation, str) or not user_confirmation.strip() or len(user_confirmation) > 2000:
                    raise StateError("需要用户对本次具体取消操作的明确确认原文。")
                snapshot = self._cancellation_snapshot(db, session)
                if snapshot["fingerprint"] != expected_fingerprint:
                    raise StateError("会话、计划或审批已变化，请重新展示取消范围并取得用户确认。")
                self._require_chat_cancellable(snapshot)
            if not session or session["state"] not in {"locked", "planned", "awaiting_approval", "restore_pending"}:
                raise StateError("Only an unexecuted edit session can be aborted")
            transactions = db.execute("SELECT * FROM transactions WHERE session_id=?", (session_id,)).fetchall()
            for transaction in transactions:
                # A completed save predating an unexecuted restore is history,
                # not a running transaction. Keep it and restore its pointer below.
                if session["state"] == "restore_pending" and transaction["id"] != session["transaction_id"]:
                    if transaction["state"] in {"saved", "saved_state_pending"}:
                        continue
                if transaction["state"] in {"cancelled", "failed_no_write", "rolled_back", "not_applied"}:
                    continue
                # Older writers classified this pre-write concurrency rejection as
                # generic failed. This exact branch runs before any business SQL.
                if transaction["state"] == "failed" and transaction["message"] in {
                    "The draft content changed after planning; transaction was not executed",
                    "The draft modification timestamp changed after planning",
                    "Database account mode or grants changed after approval planning",
                }:
                    artifact = load_artifact(transaction["artifact_path"])
                    if not artifact.get("executed"):
                        db.execute("UPDATE transactions SET state='failed_no_write' WHERE id=?", (transaction["id"],))
                        continue
                if transaction["state"] != "awaiting_approval":
                    raise StateError("Execution has been attempted or its result is uncertain; verify before releasing the session")
                try:
                    artifact = load_artifact(transaction["artifact_path"])
                except (OSError, ValueError):
                    # Pending execution must read a valid artifact before it can
                    # claim the transaction; SQLite serializes cancellation/claim.
                    continue
                if not isinstance(artifact, dict):
                    continue
                if artifact.get("execution_started") or artifact.get("executed"):
                    raise StateError("Execution has already started; the session cannot be aborted")
            now = datetime.now().isoformat(timespec="seconds")
            if expected_fingerprint is not None:
                audit = {"event": "user_authorized_cancellation", "confirmation_channel": "chat",
                         "user_confirmation": user_confirmation, "snapshot": snapshot,
                         "verified": False, "business_data_written": False}
                db.execute("INSERT INTO session_closures VALUES (?,?,?,?)",
                           (uuid.uuid4().hex, session_id, json.dumps(audit, ensure_ascii=False), now))
            db.execute("UPDATE transactions SET state='cancelled', message=?, updated_at=? WHERE session_id=? AND state='awaiting_approval'",
                       ("User authorized cancellation in chat" if expected_fingerprint is not None else "User manually aborted the edit session", now, session_id))
            db.execute("UPDATE plans SET status='cancelled' WHERE session_id=? AND status IN ('ready','blocked','frozen')", (session_id,))
            if session["state"] == "restore_pending":
                previous = db.execute("SELECT id,state FROM transactions WHERE session_id=? AND state IN ('saved','saved_state_pending') ORDER BY created_at DESC, rowid DESC LIMIT 1", (session_id,)).fetchone()
                if not previous:
                    raise StateError("恢复审批缺少原保存记录，请先核对会话结果。")
                db.execute("UPDATE sessions SET state=?, transaction_id=?, updated_at=? WHERE id=?",
                           (previous["state"], previous["id"], now, session_id))
            else:
                db.execute("UPDATE sessions SET state='aborted', updated_at=? WHERE id=?", (now, session_id))

    def release_failed_transaction(self, transaction_id: str, outcome: str, message: str) -> None:
        """Release only failures proven not written or rolled back; retain the audit."""
        if outcome not in {"failed_no_write", "rolled_back"}:
            raise StateError("A confirmed no-write or rollback outcome is required")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            transaction = db.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            if not transaction or transaction["state"] != "executing":
                raise StateError("Only the executing transaction can record its confirmed failure")
            now = datetime.now().isoformat(timespec="seconds")
            db.execute("UPDATE transactions SET state=?, message=?, updated_at=? WHERE id=?",
                       (outcome, message, now, transaction_id))
            db.execute("UPDATE plans SET status='cancelled' WHERE session_id=? AND status IN ('ready','blocked','frozen')",
                       (transaction["session_id"],))
            db.execute("UPDATE sessions SET state=?, updated_at=? WHERE id=? AND transaction_id=?",
                       (outcome, now, transaction["session_id"], transaction_id))
            if transaction["kind"] == "restore":
                previous = db.execute("SELECT id,state FROM transactions WHERE session_id=? AND state IN ('saved','saved_state_pending') ORDER BY created_at DESC,rowid DESC LIMIT 1", (transaction["session_id"],)).fetchone()
                if previous:
                    db.execute("UPDATE sessions SET state=?,transaction_id=?,updated_at=? WHERE id=?",
                               (previous["state"], previous["id"], now, transaction["session_id"]))

    def archive_session(self, session_id: str, *, conversation_id: str, owner_revision: int,
                        expected_session_state: str, expected_transaction_id: str | None,
                        expected_transaction_state: str | None) -> None:
        """Manual local closure only; caller holds window/executor locks and checks processes."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            self._assert_owner(session, conversation_id, owner_revision)
            if session["state"] not in ACTIVE_STATES or session["state"] != expected_session_state or session["transaction_id"] != expected_transaction_id:
                raise StateError("会话状态或审批流程已变化，请重新打开处理窗口。")
            transaction = db.execute("SELECT * FROM transactions WHERE id=?", (expected_transaction_id,)).fetchone()
            if (transaction["state"] if transaction else None) != expected_transaction_state:
                raise StateError("事务状态已变化，请重新打开处理窗口。")
            transactions = [dict(row) for row in db.execute("SELECT * FROM transactions WHERE session_id=?", (session_id,))]
            now = datetime.now().isoformat(timespec="seconds")
            snapshot = {"session_state": session["state"], "transaction_id": session["transaction_id"],
                        "conversation_id": conversation_id, "owner_revision": owner_revision, "transactions": transactions,
                        "verified": False, "business_data_written": False,
                        "reason": "User chose to retain current database data and release local occupancy without asserting execution success"}
            db.execute("INSERT INTO session_closures VALUES (?,?,?,?)", (uuid.uuid4().hex, session_id, json.dumps(snapshot, ensure_ascii=False), now))
            db.execute("UPDATE transactions SET state='closed_unverified',updated_at=? WHERE session_id=? AND state IN ('awaiting_approval','executing','failed','saved_state_pending')", (now, session_id))
            db.execute("UPDATE plans SET status='cancelled' WHERE session_id=? AND status IN ('ready','blocked','frozen')", (session_id,))
            db.execute("UPDATE sessions SET state='closed_unverified',updated_at=? WHERE id=?", (now, session_id))

    def reconcile_transaction(self, transaction_id: str, expected_state: str, outcome: str, message: str, *, conversation_id: str | None = "", owner_revision: int | None = None) -> None:
        """Record a database snapshot result while recovery holds the executor lock."""
        if outcome not in {"saved", "not_applied", "closed_changed"}:
            raise StateError("Unsupported recovery outcome")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            transaction = db.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            if not transaction or transaction["state"] != expected_state:
                raise StateError("事务状态已改变，请重新核对。")
            session = db.execute("SELECT * FROM sessions WHERE id=?", (transaction["session_id"],)).fetchone()
            self._assert_owner(session, conversation_id, owner_revision)
            if not session or session["transaction_id"] != transaction_id or session["state"] not in ACTIVE_STATES:
                raise StateError("会话已结束或当前事务已改变。")
            now = datetime.now().isoformat(timespec="seconds")
            db.execute("UPDATE transactions SET state=?,message=?,updated_at=? WHERE id=?", (outcome, message, now, transaction_id))
            db.execute("UPDATE plans SET status='cancelled' WHERE session_id=? AND status IN ('ready','blocked','frozen')", (transaction["session_id"],))
            db.execute("UPDATE sessions SET state=?,updated_at=? WHERE id=?", ("verified" if outcome == "saved" else outcome, now, transaction["session_id"]))

    def transfer_session(self, session_id: str, conversation_id: str, *, expected_owner: str, expected_revision: int, expected_transaction_id: str | None = None, executor_idle: bool = False) -> dict[str, Any]:
        """Caller holds the window and executor locks before changing ownership."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._conversation_exists(db, conversation_id)
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            self._assert_owner(session, expected_owner, expected_revision)
            if session["transaction_id"] != expected_transaction_id:
                raise StateError("审批流程已变化，请重新接手。")
            if session["state"] not in ACTIVE_STATES:
                raise StateError("已结束的会话无需接手，请新建编辑会话。")
            active = db.execute("SELECT id FROM sessions WHERE conversation_id=? AND id!=? AND state IN ('locked','planned','awaiting_approval','saved','saved_state_pending','verified','restore_pending')", (conversation_id, session_id)).fetchone()
            if active:
                raise StateError("当前对话已有其他编辑会话，请先结束它。")
            if session["transaction_id"]:
                transaction = db.execute("SELECT state FROM transactions WHERE id=?", (session["transaction_id"],)).fetchone()
                if transaction and transaction["state"] == "executing" and not executor_idle:
                    raise StateError("审批流程正在执行，不能转交。")
            if session["conversation_id"] != conversation_id:
                db.execute("UPDATE sessions SET conversation_id=?,owner_revision=owner_revision+1,updated_at=? WHERE id=?", (conversation_id, datetime.now().isoformat(timespec="seconds"), session_id))
        return self.get_session(session_id, include_baseline=False)

    def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = self._row(db.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone())
        if not row:
            raise StateError(f"Transaction {transaction_id!r} was not found")
        return row

    def session_transactions(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM transactions WHERE session_id=? ORDER BY id", (session_id,))]

    def update_transaction(self, transaction_id: str, state: str, message: str = "") -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE transactions SET state=?, message=?, updated_at=? WHERE id=?",
                (state, message, datetime.now().isoformat(timespec="seconds"), transaction_id),
            )
