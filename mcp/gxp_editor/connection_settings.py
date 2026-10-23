"""Shared local connection settings; no secrets are accepted by MCP or persisted in JSON."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import keyring


class SettingsError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _config_home() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def settings_path() -> Path:
    override = os.environ.get("GXP_LOWCODE_SHARED_CONFIG")
    return Path(override).expanduser().resolve() if override else _config_home() / "GxpLowcode/connection.json"


def _base_path(role: str) -> Path:
    variable = "GXP_LOWCODE_CONFIG" if role == "look" else "GXP_LOWCODE_EDITOR_CONFIG"
    if os.environ.get(variable):
        return Path(os.environ[variable]).expanduser().resolve()
    # Keep Write's existing default on macOS/Linux, too.
    base = _config_home() if role == "look" or os.name == "nt" else Path(os.environ.get("APPDATA") or Path.home() / ".config")
    return base / ("GxpLowcodeReadonly" if role == "look" else "GxpLowcodeEditor") / "database.json"


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 256 * 1024:
            raise ValueError("oversized")
        result = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(result, dict):
            raise ValueError("object required")
        return result
    except (OSError, ValueError) as exc:
        raise SettingsError("SETTINGS_INVALID", "连接配置无法读取，请检查本地设置文件；未更改配置。") from exc


def _validate_fields(values: dict) -> dict:
    cleaned = {}
    for name in ("host", "database", "look_user", "write_user"):
        value = values.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(char) < 32 for char in value):
            raise SettingsError("SETTINGS_FIELDS", "地址、库名及两个账号须为 1–200 个字符的非空文本。")
        cleaned[name] = value.strip()
    if "://" in cleaned["host"] or any(char in cleaned["host"] for char in ("/", "\\", "@")):
        raise SettingsError("SETTINGS_FIELDS", "数据库地址只填写主机名或 IP，不填写 URL 或连接串。")
    port = values.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise SettingsError("SETTINGS_FIELDS", "数据库端口须为 1–65535 的整数。")
    return {**cleaned, "port": port}


def active_settings() -> dict | None:
    result = _read_json(settings_path())
    if result is None:
        return None
    expected = {"schema_version", "revision", "host", "port", "database", "accounts"}
    if set(result) != expected or result.get("schema_version") != 1 or not re.fullmatch(r"[0-9a-f]{32}", str(result.get("revision", ""))):
        raise SettingsError("SETTINGS_INVALID", "统一连接配置格式无效；已停止使用该配置。")
    accounts = result["accounts"]
    if not isinstance(accounts, dict) or set(accounts) != {"look", "write"}:
        raise SettingsError("SETTINGS_INVALID", "统一连接配置缺少两端账号。")
    for role in ("look", "write"):
        account = accounts[role]
        if not isinstance(account, dict) or set(account) != {"user", "credential_target"}:
            raise SettingsError("SETTINGS_INVALID", "账号配置只允许用户名和系统凭据引用。")
        target = account.get("credential_target")
        if not isinstance(target, str) or not target.startswith("GxpLowcode.Connection.") or len(target) > 200:
            raise SettingsError("SETTINGS_INVALID", "系统凭据引用无效。")
    _validate_fields({**result, **{role + "_user": accounts[role]["user"] for role in accounts}})
    return result


def overlay_database(value: dict, role: str) -> dict:
    """Preserve plugin-specific account policy/timeouts and all non-DB options."""
    active = active_settings()
    if active is None:
        return value
    return {**value, **{key: active[key] for key in ("host", "port", "database")}, **active["accounts"][role]}


def _base_configs() -> dict:
    result = {}
    for role in ("look", "write"):
        raw = _read_json(_base_path(role)) or {}
        result[role] = raw.get("database", {}) if role == "write" else raw
        if not isinstance(result[role], dict):
            raise SettingsError("SETTINGS_INVALID", "现有数据库配置格式无效。")
    return result


def _revision(active: dict | None, bases: dict) -> str:
    data = json.dumps({"active": active, "bases": bases}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _editor_state_path() -> Path:
    override = os.environ.get("GXP_LOWCODE_EDITOR_STATE")
    if override:
        return Path(override).expanduser().resolve()
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/share")
    return base / "GxpLowcodeEditor/state/editor.sqlite3"


def _active_sessions() -> list[dict]:
    path = _editor_state_path()
    if not path.exists():
        return []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT id,state FROM sessions WHERE state IN ('locked','planned','awaiting_approval','saved','saved_state_pending','verified','restore_pending') LIMIT 101").fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        raise SettingsError("SETTINGS_SESSION_CHECK", "无法核对编辑会话状态，暂不允许切换连接。") from exc


def settings_status() -> dict:
    active, bases = active_settings(), _base_configs()
    primary = active or bases["look"] or bases["write"]
    settings = {key: primary.get(key, 3306 if key == "port" else "") for key in ("host", "port", "database")}
    for role in ("look", "write"):
        settings[role + "_user"] = active["accounts"][role]["user"] if active else bases[role].get("user", "")
    return {"ok": True, "configured": active is not None, "revision": _revision(active, bases),
            "settings": settings, "config_path": str(settings_path()), "active_edit_sessions": _active_sessions(),
            "legacy_connections": {role: {key: base.get(key) for key in ("host", "port", "database", "user")} for role, base in bases.items()},
            "next_step": "统一设置同时应用于 Look 和 Write；密码只在本地窗口输入。已有编辑流程须先正常结束，不能自动取消。"}


@contextmanager
def _settings_lock():
    path = settings_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SettingsError("SETTINGS_BUSY", "另一处正在修改连接，请稍后重新读取设置。") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _secure_keyring() -> None:
    backend = keyring.get_keyring()
    module = type(backend).__module__.lower()
    if backend.priority <= 0 or any(word in module for word in ("plaintext", "fail", "null")):
        raise SettingsError("SETTINGS_KEYRING", "系统安全凭据库不可用；未保存密码或连接设置。")
    # Chainer backends may choose a plaintext provider; accept only OS providers.
    if not any(word in module for word in ("windows", "macos", "secretservice", "libsecret")):
        raise SettingsError("SETTINGS_KEYRING", "请启用 Windows 凭据管理器、macOS Keychain 或 Linux Secret Service。")


def _probe(values: dict, passwords: dict) -> None:
    import pymysql
    for role in ("look", "write"):
        try:
            connection = pymysql.connect(host=values["host"], port=values["port"], database=values["database"],
                user=values[role + "_user"], password=passwords[role], charset="utf8mb4", autocommit=False,
                connect_timeout=5, read_timeout=5, write_timeout=5)
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET SESSION TRANSACTION READ ONLY")
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                connection.rollback()
            finally:
                connection.close()
        except Exception as exc:
            raise SettingsError("SETTINGS_CONNECTION", f"{role.title()} 账号连接测试失败。请检查地址、库名、账号与密码；原配置保留。") from exc


def _atomic_save(profile: dict) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".connection-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(profile, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def update_settings(changes: dict, expected_revision: str, *, look_password: str | None = None,
                    write_password: str | None = None, test_only: bool = False) -> dict:
    staged = []
    try:
        allowed = {"host", "port", "database", "look_user", "write_user"}
        if not isinstance(changes, dict) or set(changes) - allowed:
            raise SettingsError("SETTINGS_FIELDS", "只接受地址、端口、库名及两端用户名；密码不能通过工具或命令参数提交。")
        with _settings_lock():
            before = settings_status()
            if not isinstance(expected_revision, str) or expected_revision != before["revision"]:
                raise SettingsError("SETTINGS_CONFLICT", "设置已变化，请重新读取后再提交；未覆盖其他修改。")
            values = _validate_fields({**before["settings"], **changes})
            active, bases = active_settings(), _base_configs()
            if not test_only and before["active_edit_sessions"]:
                return {"ok": False, "changed": False, "error_code": "SETTINGS_ACTIVE_SESSIONS",
                        "error": "仍有编辑或审批流程，请先正常结束；未自动取消任何流程或更改连接。",
                        "active_edit_sessions": before["active_edit_sessions"]}
            _secure_keyring()
            passwords = {}
            for role, supplied in (("look", look_password), ("write", write_password)):
                account = active["accounts"][role] if active else bases[role]
                if supplied is not None and (not isinstance(supplied, str) or not supplied):
                    raise SettingsError("SETTINGS_CREDENTIALS", "密码不能为空。")
                if supplied is None:
                    if values[role + "_user"] != account.get("user"):
                        raise SettingsError("SETTINGS_CREDENTIALS", "更换账号需要在本地窗口输入对应密码。")
                    target = account.get("credential_target") or ("Codex.GxpLowcodeReadonly" if role == "look" else "Codex.GxpLowcodeEditor.Database")
                    supplied = keyring.get_password(target, values[role + "_user"])
                if not supplied:
                    raise SettingsError("SETTINGS_CREDENTIALS", "缺少系统凭据，请打开本地设置窗口输入密码。")
                passwords[role] = supplied
            _probe(values, passwords)
            if test_only:
                return {"ok": True, "changed": False, "tested": True, "message": "两端只读连接测试通过；未保存配置或密码。"}
            # Test first; then create fresh references so failure never replaces
            # the credential used by the previous active configuration.
            revision = secrets.token_hex(16)
            profile = {"schema_version": 1, "revision": revision,
                       **{key: values[key] for key in ("host", "port", "database")}, "accounts": {}}
            for role in ("look", "write"):
                account = {"user": values[role + "_user"], "credential_target": f"GxpLowcode.Connection.{revision}.{role}"}
                keyring.set_password(account["credential_target"], account["user"], passwords[role])
                staged.append(account)
                profile["accounts"][role] = account
            # Recheck occupancy after connection testing; new sessions capture
            # an endpoint fingerprint and cannot execute against another DB.
            if _active_sessions():
                raise SettingsError("SETTINGS_ACTIVE_SESSIONS", "测试期间出现新的编辑流程，连接未切换；请结束流程后重试。")
            _atomic_save(profile)
            staged.clear()
            return {"ok": True, "changed": True, "tested": True, "revision": _revision(profile, bases),
                    "settings": values, "config_path": str(settings_path()),
                    "message": "两端连接已统一更新；后续调用使用新连接。请重新检查 Write 权限与编译就绪状态。"}
    except SettingsError as exc:
        return {"ok": False, "changed": False, "error_code": exc.code, "error": str(exc)}
    except Exception:
        return {"ok": False, "changed": False, "error_code": "SETTINGS_FAILED", "error": "连接设置未保存，请检查本地权限或系统凭据库。"}
    finally:
        for account in staged:
            try:
                keyring.delete_password(account["credential_target"], account["user"])
            except Exception:
                pass


def open_settings() -> dict:
    package = __package__
    python = Path(sys.executable)
    if os.name == "nt" and python.with_name("pythonw.exe").exists():
        python = python.with_name("pythonw.exe")
    with tempfile.TemporaryDirectory(prefix="gxp-settings-ui-") as temporary:
        receipt = Path(temporary) / "ready.json"
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]) + os.pathsep + os.environ.get("PYTHONPATH", ""),
               "GXP_LOWCODE_SETTINGS_RECEIPT": str(receipt)}
        process = subprocess.Popen([str(python), "-m", package + ".connection_settings_ui"], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if receipt.exists():
                try:
                    ready = _read_json(receipt) == {"ready": True}
                except SettingsError:
                    # The UI may still be flushing its tiny acknowledgement.
                    # Retry within the same bounded startup confirmation wait.
                    ready = False
                if ready:
                    return {"ok": True, "opened": True, "process_id": process.pid,
                            "message": "统一连接设置窗口已打开。密码在窗口隐藏输入；测试通过并保存后才切换。"}
            if process.poll() is not None:
                break
            time.sleep(0.05)
        return {"ok": False, "opened": False, "error_code": "SETTINGS_WINDOW_UNCONFIRMED",
                "error": "尚未确认设置窗口已显示，请使用桌面脚本打开并检查本地 Python/Tk 环境。"}


def manage_settings(operation: str = "status", changes: dict | None = None, expected_revision: str = "") -> dict:
    try:
        if operation == "update":
            return update_settings(changes if changes is not None else {}, expected_revision)
        if changes is not None or expected_revision:
            raise SettingsError("SETTINGS_FIELDS", "修改参数仅可用于 update；status/open 不会更改配置。")
        if operation == "status":
            return settings_status()
        if operation == "open":
            return open_settings()
        raise SettingsError("SETTINGS_OPERATION", "操作必须是 status、open 或 update。")
    except SettingsError as exc:
        return {"ok": False, "error_code": exc.code, "error": str(exc)}
    except Exception:
        return {"ok": False, "error_code": "SETTINGS_UNAVAILABLE", "error": "本地连接设置暂不可用。"}


def cli_main() -> int:
    parser = argparse.ArgumentParser(description="Look / Write 统一开发库设置（密码仅在窗口输入）")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--expected-revision", default="")
    for name in ("host", "database", "look-user", "write-user"):
        parser.add_argument("--" + name)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.status or args.update:
        changes = {name: getattr(args, name) for name in ("host", "port", "database", "look_user", "write_user") if getattr(args, name) is not None}
        result = manage_settings("update" if args.update else "status", changes if args.update else None, args.expected_revision)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    from importlib import import_module
    return import_module(__package__ + ".connection_settings_ui").main()
