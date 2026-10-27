"""Short SQLite control transactions, separate from a window's lifetime OS lock."""
from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any

from .hashing import sha256_json, sha256_text
from .state import StateError

TOKEN_ENV = "GXP_EDITOR_QUEUE_LAUNCH_TOKEN"
GENERATION_ENV = "GXP_EDITOR_QUEUE_GENERATION"


@contextmanager
def _control(state):
    try:
        with state._connection() as db:
            db.execute("PRAGMA busy_timeout=0")
            yield db
    except sqlite3.OperationalError as exc:
        raise StateError("Queue control is unavailable or busy; read the current queue before retrying") from exc


def snapshot_in(db, queue_id: str, conversation_id: str) -> dict[str, Any]:
    queue = db.execute("SELECT conversation_id FROM edit_queues WHERE id=?", (queue_id,)).fetchone()
    if not queue or queue["conversation_id"] != conversation_id:
        raise StateError("审批队列不存在或属于其他对话。")
    control = db.execute("SELECT * FROM queue_runtime WHERE queue_id=?", (queue_id,)).fetchone()
    rows = db.execute("""SELECT s.id,s.state,s.plan_id,s.transaction_id,s.owner_revision,s.conversation_id,
        i.owner_revision AS queued_owner_revision,i.position,t.state AS transaction_state,t.artifact_path
        FROM edit_queue_items i JOIN sessions s ON s.id=i.session_id
        LEFT JOIN transactions t ON t.id=s.transaction_id WHERE i.queue_id=? ORDER BY i.position,s.id""", (queue_id,)).fetchall()
    value = {"schema_version": 1, "queue_id": queue_id, "conversation_id": conversation_id,
             "revision": control["revision"] if control else 0,
             "generation": control["generation"] if control else 0,
             "launch_token": control["launch_token"] if control else "",
             "launch_state": control["launch_state"] if control else "idle",
             "reserved_revision": control["reserved_revision"] if control else -1,
             "items": [dict(row) for row in rows]}
    value["sha256"] = sha256_json(value)
    return value


def queue_snapshot(state, queue_id: str, conversation_id: str) -> dict[str, Any]:
    with _control(state) as db:
        db.execute("BEGIN")
        return snapshot_in(db, queue_id, conversation_id)


def reserve_launch(state, queue_id: str, conversation_id: str, expected: dict, *, previous_exited=False) -> dict:
    with _control(state) as db:
        db.execute("BEGIN IMMEDIATE")
        current = snapshot_in(db, queue_id, conversation_id)
        if current["sha256"] != expected["sha256"]:
            raise StateError("Queue changed during preflight; inspect it again")
        if current["launch_state"] in {"reserved", "active"}:
            proof = previous_exited if isinstance(previous_exited, dict) else {}
            if (proof.get("generation") != current["generation"]
                    or proof.get("launch_token_sha256") != sha256_text(current["launch_token"])):
                raise StateError("Queue launch is already reserved; its exact generation must be confirmed exited before retry")
        if any(row.get("transaction_state") in {"executing", "failed", "saved_state_pending"} for row in current["items"]):
            raise StateError("Queue contains an executing or uncertain transaction; reconcile it first")
        generation, token = current["generation"] + 1, uuid.uuid4().hex
        db.execute("UPDATE queue_runtime SET generation=?,launch_token=?,launch_state='reserved',reserved_revision=revision WHERE queue_id=?",
                   (generation, token, queue_id))
        return {"queue_id": queue_id, "generation": generation, "launch_token": token}


def validate_in(db, queue_id: str, *, token=None, generation=None) -> None:
    control = db.execute("SELECT generation,launch_token,launch_state FROM queue_runtime WHERE queue_id=?", (queue_id,)).fetchone()
    if not control or control["generation"] == 0:
        return  # Legacy/manual queues have not yet entered the generation protocol.
    token = os.environ.get(TOKEN_ENV, "") if token is None else token
    generation = os.environ.get(GENERATION_ENV, "") if generation is None else generation
    if (str(control["generation"]) != str(generation) or control["launch_token"] != token
            or control["launch_state"] not in {"reserved", "active"}):
        raise StateError("Queue window generation changed; this window cannot advance or execute")


def claim_window(state, queue_id: str, conversation_id: str) -> None:
    with _control(state) as db:
        db.execute("BEGIN IMMEDIATE")
        state._assert_queue_owner(db, queue_id, conversation_id)
        validate_in(db, queue_id)
        control = db.execute("SELECT revision,reserved_revision,launch_state FROM queue_runtime WHERE queue_id=?", (queue_id,)).fetchone()
        if control and control["launch_state"] == "reserved" and control["reserved_revision"] != control["revision"]:
            raise StateError("Queue changed after launch reservation; run preflight again")
        db.execute("UPDATE queue_runtime SET launch_state='active' WHERE queue_id=? AND generation>0", (queue_id,))


def validate_window(state, queue_id: str, conversation_id: str) -> None:
    with _control(state) as db:
        db.execute("BEGIN")
        state._assert_queue_owner(db, queue_id, conversation_id)
        validate_in(db, queue_id)


def release_launch(state, reservation: dict) -> None:
    with _control(state) as db:
        db.execute("UPDATE queue_runtime SET launch_state='closed' WHERE queue_id=? AND generation=? AND launch_token=?",
                   (reservation["queue_id"], reservation["generation"], reservation["launch_token"]))
