"""Isolated form state, ownership, CAS transitions and immutable signed backups."""
from __future__ import annotations

import hmac
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .common import DOMAIN, FormError, atomic_json, canonical, digest, runtime_root, timestamp


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else runtime_root()
        if root is None:
            try:
                from gxp_project_context import current_database_scope
            except ModuleNotFoundError as error:
                if error.name != "gxp_project_context":
                    raise
                scope = None
            else:
                scope = current_database_scope(role="write")
            if scope is not None:
                if not isinstance(scope, str) or len(scope) != 64 or any(ch not in "0123456789abcdef" for ch in scope):
                    raise FormError("form_database_scope_invalid", "当前数据库的本地存储标识无效。")
                self.root = self.root / "databases" / scope
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.sqlite3"
        with self.transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL,id TEXT NOT NULL,owner TEXT NOT NULL,revision INTEGER NOT NULL,document TEXT NOT NULL,PRIMARY KEY(kind,id))")
            connection.execute("CREATE TABLE IF NOT EXISTS leases (target TEXT PRIMARY KEY,session_id TEXT UNIQUE NOT NULL)")

    @contextmanager
    def transaction(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def insert(self, kind: str, document: dict, owner: str, connection=None) -> dict:
        if connection is None:
            with self.transaction() as active:
                return self.insert(kind, document, owner, active)
        value = {**document, "id": document.get("id") or uuid.uuid4().hex,
                 "domain": DOMAIN, "owner": owner, "revision": 1, "created_at": timestamp()}
        connection.execute("INSERT INTO records VALUES (?,?,?,?,?)", (kind, value["id"], owner, 1, canonical(value)))
        return value

    def get(self, kind: str, record_id: str, owner: str, connection=None) -> dict:
        if not isinstance(record_id, str) or len(record_id) != 32 or any(ch not in "0123456789abcdef" for ch in record_id):
            raise FormError("not_owned", "表单记录不存在或不属于当前对话。")
        if connection is None:
            with self.transaction() as active:
                return self.get(kind, record_id, owner, active)
        row = connection.execute("SELECT owner,document FROM records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
        if row is None or row[0] != owner:
            raise FormError("not_owned", "表单记录不存在或不属于当前对话。")
        value = json.loads(row[1])
        if value.get("domain") != DOMAIN:
            raise FormError("wrong_domain", "不能将编排工件用于表单编辑。")
        return value

    def replace(self, kind: str, document: dict, owner: str, expected_revision: int, connection=None) -> dict:
        if connection is None:
            with self.transaction() as active:
                return self.replace(kind, document, owner, expected_revision, active)
        if document.get("domain") != DOMAIN or document.get("owner") != owner:
            raise FormError("wrong_domain", "表单状态归属无效。")
        value = {**document, "revision": expected_revision + 1, "updated_at": timestamp()}
        changed = connection.execute("UPDATE records SET revision=?,document=? WHERE kind=? AND id=? AND owner=? AND revision=?",
                                     (value["revision"], canonical(value), kind, value["id"], owner, expected_revision)).rowcount
        if changed != 1:
            raise FormError("state_changed", "表单流程已变化，请读取当前状态。")
        return value

    def create_conversation(self, label: str = "") -> dict:
        marker = uuid.uuid4().hex
        return self.insert("conversation", {"id": marker, "label": label[:160]}, marker)

    def require_conversation(self, marker: str) -> dict:
        if not marker:
            raise FormError("conversation_required", "请先注册独立表单对话。")
        return self.get("conversation", marker, marker)

    def create_session(self, owner: str, target: dict, snapshot: dict, backup: dict) -> dict:
        self.require_conversation(owner)
        with self.transaction() as connection:
            session = self.insert("session", {"target": target, "snapshot": snapshot, "backup": backup,
                                                "state": "active", "verified": False, "session_released": False}, owner, connection)
            try:
                identity = {key: target.get(key) for key in ("app_id", "site_out_id", "page_id", "data_version_id", "platform")}
                url = urlsplit(str(target.get("base_url") or ""))
                identity["origin"] = [url.scheme.lower(), (url.hostname or "").lower(), url.port or (443 if url.scheme.lower() == "https" else 80)]
                connection.execute("INSERT INTO leases VALUES (?,?)", (digest(identity), session["id"]))
            except sqlite3.IntegrityError as error:
                raise FormError("page_busy", "同一页面版本已有活动表单编辑会话。") from error
            return session

    def release(self, session: dict, connection) -> None:
        connection.execute("DELETE FROM leases WHERE session_id=?", (session["id"],))

    def save_backup(self, snapshot: dict, secret: bytes) -> dict:
        backup_id = uuid.uuid4().hex
        value = {"domain": DOMAIN, "snapshot": snapshot, "snapshot_hash": digest(snapshot)}
        signature = hmac.new(secret, canonical(value).encode(), "sha256").hexdigest()
        path = self.root / "backups" / (backup_id + ".json")
        atomic_json(path, {**value, "signature": signature})
        return {"id": backup_id, "snapshot_hash": value["snapshot_hash"], "signature": signature}

    def load_backup(self, backup: dict, secret: bytes) -> dict:
        backup_id = backup.get("id", "")
        if len(backup_id) != 32 or any(ch not in "0123456789abcdef" for ch in backup_id):
            raise FormError("backup_invalid", "备份身份无效。")
        try:
            record = json.loads((self.root / "backups" / (backup_id + ".json")).read_text(encoding="utf-8"))
            signature = record.pop("signature")
        except (OSError, ValueError, KeyError) as error:
            raise FormError("backup_invalid", "备份缺失或损坏。") from error
        expected = hmac.new(secret, canonical(record).encode(), "sha256").hexdigest()
        if (record.get("domain") != DOMAIN or not hmac.compare_digest(signature, expected)
                or signature != backup.get("signature") or digest(record.get("snapshot")) != backup.get("snapshot_hash")):
            raise FormError("backup_invalid", "备份内容指纹或签名不一致。")
        return record["snapshot"]
