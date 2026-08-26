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

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def active_session(self) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        with self._connection() as db:
            return self._row(db.execute(f"SELECT * FROM sessions WHERE state IN ({placeholders}) ORDER BY created_at LIMIT 1", ACTIVE_STATES).fetchone())

    def create_session(self, draft: dict[str, Any], backup_id: str, compiler_hash: str) -> dict[str, Any]:
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
 compiler_hash,plan_id,transaction_id,created_at,updated_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            db.execute("UPDATE plans SET status='superseded' WHERE session_id=? AND status='ready'", (session_id,))
            db.execute(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?)",
                (plan_id, session_id, "ready", artifact_path, base_hash, new_hash, compiler_hash, datetime.now().isoformat(timespec="seconds")),
            )
        self.update_session(session_id, "planned", plan_id=plan_id)
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
            active = db.execute(
                "SELECT id FROM transactions WHERE session_id=? AND state IN ('awaiting_approval','executing') LIMIT 1",
                (session_id,),
            ).fetchone()
            if active:
                raise StateError(f"Session already has an active approval transaction: {active['id']}")
            db.execute(
                "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?)",
                (transaction_id, session_id, plan_id, kind, "awaiting_approval", artifact_path, "", now, now),
            )
        self.update_session(session_id, "awaiting_approval" if kind == "write" else "restore_pending", transaction_id=transaction_id)
        return transaction_id

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
