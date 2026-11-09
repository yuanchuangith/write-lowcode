"""Read-only inventory of local editing occupancy; never opens a business DB."""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path


ACTION_STATES = ("locked", "planned", "awaiting_approval", "saved", "saved_state_pending",
                 "verified", "restore_pending")
PENDING_TRANSACTIONS = ("awaiting_approval", "executing", "failed", "saved_state_pending")


class LocalSessionError(RuntimeError):
    code = "SETTINGS_SESSION_CHECK"


def _state_paths():
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/share")
    configured = os.environ.get("GXP_LOWCODE_EDITOR_STATE")
    action = Path(configured).expanduser().resolve() if configured else base / "GxpLowcodeEditor/state/editor.sqlite3"
    configured = os.environ.get("GXP_FORM_CONFIG_ROOT")
    form = Path(configured).expanduser().resolve() if configured else base / "GxpLowcodeEditor/form-config"
    paths = [(action, "action", ""), (form / "state.sqlite3", "form", "")]
    partitioned = form / "databases"
    if partitioned.is_dir():
        for directory in sorted(partitioned.iterdir()):
            if directory.is_dir() and re.fullmatch(r"[0-9a-f]{64}", directory.name):
                paths.append((directory / "state.sqlite3", "form", directory.name))
    return paths


def _columns(db, table):
    return {row["name"] for row in db.execute("PRAGMA table_info(" + table + ")")}


def _action_rows(db, limit):
    columns = _columns(db, "sessions")
    if not {"id", "state"}.issubset(columns):
        raise LocalSessionError("无法核对动作编辑状态；未更改任何流程。")
    fields = [name if name in columns else "'' AS " + name for name in
              ("id", "state", "conversation_id", "action_name", "action_code", "updated_at", "database_scope")]
    placeholders = ",".join("?" for _ in ACTION_STATES)
    rows = [dict(row, domain="action", kind="session") for row in db.execute(
        "SELECT " + ",".join(fields) + " FROM sessions WHERE state IN (" + placeholders + ") LIMIT ?",
        (*ACTION_STATES, limit + 1))]
    transaction_columns = _columns(db, "transactions")
    if {"id", "state", "session_id"}.issubset(transaction_columns):
        active = {row["id"]: row for row in rows}
        placeholders = ",".join("?" for _ in PENDING_TRANSACTIONS)
        updated = "updated_at" if "updated_at" in transaction_columns else "'' AS updated_at"
        for item in db.execute("SELECT id,state,session_id," + updated + " FROM transactions WHERE state IN ("
                               + placeholders + ") LIMIT ?", (*PENDING_TRANSACTIONS, limit + 1)):
            if item["session_id"] in active:
                if item["state"] in {"executing", "failed", "saved_state_pending"}:
                    active[item["session_id"]]["state"] = item["state"]
            else:
                rows.append({**dict(item), "domain": "action", "kind": "transaction", "conversation_id": "",
                             "action_name": "待核对审批记录", "action_code": ""})
    return rows


def _form_rows(db, limit):
    if not {"kind", "id", "owner", "document"}.issubset(_columns(db, "records")):
        raise LocalSessionError("无法核对表单编辑状态；未更改任何流程。")
    # SQLite projects only the required metadata; snapshots and request bodies
    # never leave the local state file through this inventory.
    fields = ("id,owner,json_extract(document,'$.state') AS state,"
              "json_extract(document,'$.updated_at') AS updated_at,"
              "json_extract(document,'$.target.route') AS route,"
              "json_extract(document,'$.target.page_id') AS page_id")
    rows = [{"id": row["id"], "state": row["state"] or "unknown", "domain": "form", "kind": "session",
             "conversation_id": row["owner"], "action_name": "表单 " + str(row["route"] or row["page_id"] or row["id"]),
             "action_code": "", "updated_at": row["updated_at"] or ""}
            for row in db.execute("SELECT " + fields + " FROM records WHERE kind='session' "
                                  "AND COALESCE(json_extract(document,'$.session_released'),0)!=1 "
                                  "AND COALESCE(json_extract(document,'$.state'),'')!='cancelled' LIMIT ?", (limit + 1,))]
    active_ids = {row["id"] for row in rows}
    for row in db.execute("SELECT id,owner,json_extract(document,'$.state') AS state,"
                          "json_extract(document,'$.session_id') AS session_id,"
                          "json_extract(document,'$.updated_at') AS updated_at FROM records WHERE kind='write' "
                          "AND json_extract(document,'$.state') IN ('pending','approved','executing','uncertain') "
                          "AND COALESCE(json_extract(document,'$.session_released'),0)!=1 LIMIT ?", (limit + 1,)):
        if row["session_id"] not in active_ids:
            rows.append({"id": row["id"], "session_id": row["session_id"], "state": row["state"], "domain": "form",
                         "kind": "write", "conversation_id": row["owner"], "action_name": "待核对表单保存",
                         "action_code": "", "updated_at": row["updated_at"] or ""})
    return rows


def active_sessions(limit=1000):
    """List legacy and database-partitioned local blockers without migration."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Local session limit must be between 1 and 1000")
    result = []
    try:
        for path, domain, scope in _state_paths():
            if not path.exists():
                continue
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                rows = _action_rows(db, limit) if domain == "action" else _form_rows(db, limit)
            for row in rows:
                if scope:
                    row["database_scope"] = scope
                result.append(row)
            if len(result) > limit:
                raise LocalSessionError("本地未结束流程过多，列表不完整；未更改任何流程。")
    except (OSError, sqlite3.Error, ValueError) as error:
        raise LocalSessionError("无法读取本地流程状态；未更改任何记录。") from error
    return result


list_active_sessions = active_sessions
