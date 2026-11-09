"""Small, local database-profile store. Contains references, never passwords.

Kept identical in Look and Write so either plugin can resolve saved connections.
Platform settings, compilation and approval workflows remain plugin-owned.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def database_scope(database: dict) -> str:
    host, name, port = database.get("host"), database.get("database"), database.get("port")
    if (not isinstance(host, str) or not host.strip() or not isinstance(name, str)
            or not name.strip() or type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("数据库地址、端口或库名无效。")
    return _digest({"host": host.strip().lower(), "port": port, "database": name.strip()})


def _backend(role: str):
    if role not in ("look", "write"):
        raise ValueError("Unknown database role")
    from importlib import import_module
    return import_module("gxp_core.connection_settings" if role == "look" else "gxp_editor.connection_settings")


def current_database_scope(role: str = "look") -> str | None:
    backend = _backend(role)
    active = backend.active_settings()
    database = active if active else backend._base_configs()[role]
    return database_scope(database) if database else None


def profiles_root(settings_path: Path) -> Path:
    override = os.environ.get("GXP_PROJECT_CONFIG_ROOT")
    return Path(override).expanduser().resolve() if override else settings_path.with_suffix(".projects")


def _read(path: Path) -> dict:
    try:
        if path.stat().st_size > 256 * 1024:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError) as exc:
        raise ValueError("已保存的数据库配置无法读取；原配置未更改。") from exc


def _registry(settings_path: Path) -> dict:
    path = profiles_root(settings_path) / "projects.json"
    value = _read(path) if path.exists() else {"schema_version": 2, "projects": {}}
    if value.get("schema_version") != 2 or not isinstance(value.get("projects"), dict):
        raise ValueError("已保存的数据库配置目录格式无效。")
    return value


def load_profile(settings_path: Path, project_id: str, revision: str | None = None) -> dict:
    if not isinstance(project_id, str) or not re.fullmatch(r"[0-9a-f]{64}", project_id):
        raise ValueError("数据库配置标识无效。")
    if revision is None:
        revision = (_registry(settings_path)["projects"].get(project_id) or {}).get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ValueError("数据库配置版本无效。")
    profile = _read(profiles_root(settings_path) / "profiles" / project_id / (revision + ".json"))
    content = {key: value for key, value in profile.items() if key != "revision"}
    if (profile.get("schema_version") != 2 or profile.get("revision") != revision
            or _digest(content) != revision or profile.get("project_id") != project_id
            or database_scope(profile.get("database") or {}) != project_id):
        raise ValueError("数据库配置完整性校验失败。")
    accounts = profile.get("accounts")
    if not isinstance(accounts, dict) or set(accounts) != {"look", "write"}:
        raise ValueError("数据库配置缺少两端账户。")
    for role, account in accounts.items():
        if not isinstance(account, dict) or not isinstance(account.get("user"), str) or not account["user"].strip():
            raise ValueError("数据库账户无效。")
        target = account.get("credential_target")
        if not isinstance(target, str) or not target or len(target) > 200:
            raise ValueError("数据库凭据引用无效。")
        if role == "write":
            mode, grants = account.get("account_mode"), account.get("expected_grants_sha256", "")
            if mode is not None and mode not in {"dedicated", "shared_restricted", "shared_unrestricted"}:
                raise ValueError("Write 账户模式无效。")
            if grants and (not isinstance(grants, str) or not re.fullmatch(r"[0-9a-f]{64}", grants)):
                raise ValueError("Write 权限指纹无效。")
    return profile


def resolve_active(pointer: dict, settings_path: Path) -> dict:
    if not isinstance(pointer.get("profile_revision"), str) or not re.fullmatch(r"[0-9a-f]{64}", pointer["profile_revision"]):
        raise ValueError("活动数据库配置缺少明确版本。")
    profile = load_profile(settings_path, pointer.get("project_id"), pointer.get("profile_revision"))
    accounts = {}
    for role, account in profile["accounts"].items():
        keys = ("user", "credential_target", "account_mode", "expected_grants_sha256") if role == "write" else ("user", "credential_target")
        accounts[role] = {key: account[key] for key in keys if key in account}
    return {"schema_version": 1, "revision": _digest(pointer)[:32],
            **{key: profile["database"][key] for key in ("host", "port", "database")}, "accounts": accounts}


def list_profiles(settings_path: Path) -> list[dict]:
    result = []
    for project_id, entry in _registry(settings_path)["projects"].items():
        profile = load_profile(settings_path, project_id, entry.get("revision"))
        result.append({key: profile[key] for key in ("project_id", "revision", "name", "database")})
    return sorted(result, key=lambda item: (item["name"], item["project_id"]))


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_profile(settings_path: Path, name: str, database: dict, accounts: dict) -> dict:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
        raise ValueError("请填写 1–200 个字符的配置名称。")
    project_id = database_scope(database)
    registry = _registry(settings_path)
    existing = registry["projects"].get(project_id)
    # Preserve any older platform metadata as opaque history; never apply it.
    profile = load_profile(settings_path, project_id, existing["revision"]) if existing else {}
    profile = {**profile, "schema_version": 2, "project_id": project_id,
               "name": name.strip(), "database": {key: database[key] for key in ("host", "port", "database")},
               "accounts": accounts}
    profile.pop("revision", None)
    profile["revision"] = _digest(profile)
    path = profiles_root(settings_path) / "profiles" / project_id / (profile["revision"] + ".json")
    if not path.exists():
        _atomic_json(path, profile)
    load_profile(settings_path, project_id, profile["revision"])
    registry["projects"][project_id] = {"name": profile["name"], "revision": profile["revision"]}
    _atomic_json(profiles_root(settings_path) / "projects.json", registry)
    return {key: profile[key] for key in ("project_id", "revision", "name", "database")}
