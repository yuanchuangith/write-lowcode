from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import runtime_root


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
            columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
            if "session_mode" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN session_mode TEXT NOT NULL DEFAULT 'edit'")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def active_session(self) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        with self._connection() as db:
            return self._row(db.execute(f"SELECT * FROM sessions WHERE state IN ({placeholders}) ORDER BY created_at LIMIT 1", ACTIVE_STATES).fetchone())

    def create_session(self, draft: dict[str, Any], backup_id: str, compiler_hash: str, *, session_mode: str = "edit") -> dict[str, Any]:
        if session_mode not in {"edit", "reference_repair"}:
            raise StateError("Unsupported session mode")
        now = datetime.now().isoformat(timespec="seconds")
        session_id = uuid.uuid4().hex
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in ACTIVE_STATES)
            active = db.execute(f"SELECT id, ref_id FROM sessions WHERE state IN ({placeholders}) LIMIT 1", ACTIVE_STATES).fetchone()
            if active:
                raise StateError(f"Another edit session is active: {active['id']} ref_Id={active['ref_id']}")
            metadata = draft.get("metadata") or {}
            db.execute(
                """
INSERT INTO sessions
(id,ref_id,design_id,action_code,action_name,state,baseline_json,backup_id,
 compiler_hash,plan_id,transaction_id,created_at,updated_at,session_mode)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                ),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = self._row(db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())
        if not row:
            raise StateError(f"Session {session_id!r} was not found")
        row["baseline"] = json.loads(row.pop("baseline_json"))
        return row

    def update_session(self, session_id: str, state: str, **fields: Any) -> None:
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
            db.execute(f"UPDATE sessions SET {', '.join(assignments)} WHERE id=?", values)

    def create_plan(self, session_id: str, artifact_path: str, base_hash: str, new_hash: str, compiler_hash: str) -> str:
        plan_id = uuid.uuid4().hex
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT state FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session or session["state"] not in {"locked", "planned"}:
                raise StateError("Session can no longer be planned")
            db.execute("UPDATE plans SET status='superseded' WHERE session_id=? AND status='ready'", (session_id,))
            db.execute(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?)",
                (plan_id, session_id, "ready", artifact_path, base_hash, new_hash, compiler_hash, datetime.now().isoformat(timespec="seconds")),
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
            session = db.execute("SELECT state, plan_id FROM sessions WHERE id=?", (session_id,)).fetchone()
            allowed = {"planned"} if kind == "write" else {"saved", "saved_state_pending", "verified"}
            if not session or session["state"] not in allowed or (kind == "write" and session["plan_id"] != plan_id):
                raise StateError("Session can no longer enter approval")
            db.execute(
                "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?)",
                (transaction_id, session_id, plan_id, kind, "awaiting_approval", artifact_path, "", now, now),
            )
            db.execute("UPDATE sessions SET state=?, transaction_id=?, updated_at=? WHERE id=?",
                       ("awaiting_approval" if kind == "write" else "restore_pending", transaction_id, now, session_id))
        return transaction_id

    def claim_transaction(self, transaction_id: str) -> None:
        """Serialize execution against manual cancellation across processes."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            affected = db.execute(
                "UPDATE transactions SET state='executing', updated_at=? WHERE id=? AND state='awaiting_approval' "
                "AND session_id IN (SELECT id FROM sessions WHERE state IN ('awaiting_approval','restore_pending'))",
                (datetime.now().isoformat(timespec="seconds"), transaction_id),
            ).rowcount
            if affected != 1:
                raise StateError("Transaction is no longer awaiting approval; execution was blocked")

    def abort_session(self, session_id: str) -> None:
        """Called only by the local confirmation UI; retain backups and audit records."""
        from .approval import load_artifact

        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT state FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session or session["state"] not in {"locked", "planned", "awaiting_approval"}:
                raise StateError("Only an unexecuted edit session can be aborted")
            transactions = db.execute("SELECT * FROM transactions WHERE session_id=?", (session_id,)).fetchall()
            for transaction in transactions:
                if transaction["state"] != "awaiting_approval":
                    raise StateError("Execution has been attempted or its result is uncertain; verify before releasing the session")
                artifact = load_artifact(transaction["artifact_path"])
                if artifact.get("execution_started") or artifact.get("executed"):
                    raise StateError("Execution has already started; the session cannot be aborted")
            now = datetime.now().isoformat(timespec="seconds")
            db.execute("UPDATE transactions SET state='cancelled', message=?, updated_at=? WHERE session_id=?",
                       ("User manually aborted the edit session", now, session_id))
            db.execute("UPDATE plans SET status='cancelled' WHERE session_id=? AND status='ready'", (session_id,))
            db.execute("UPDATE sessions SET state='aborted', updated_at=? WHERE id=?", (now, session_id))

    def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = self._row(db.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone())
        if not row:
            raise StateError(f"Transaction {transaction_id!r} was not found")
        return row

    def update_transaction(self, transaction_id: str, state: str, message: str = "") -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE transactions SET state=?, message=?, updated_at=? WHERE id=?",
                (state, message, datetime.now().isoformat(timespec="seconds"), transaction_id),
            )
