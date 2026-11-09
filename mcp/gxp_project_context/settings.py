"""Versioned project profiles and atomic activation shared by all local tools."""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import keyring

from .context import (
    CONTRACT_VERSION, ProjectContext, ProjectError, _base_config, _business_scope,
    _plain, active_path, atomic_json, base_config_path, config_root, database_scope, digest,
    get_context, load_profile, operation_gate, read_json, runtime_root,
    connection_identity, project_active_settings, project_metadata_path,
)


def registry() -> dict:
    result = read_json(config_root() / "projects.json")
    if result is None:
        return {"schema_version": CONTRACT_VERSION, "projects": {}}
    if result.get("schema_version") != CONTRACT_VERSION or not isinstance(result.get("projects"), dict):
        raise ProjectError("PROJECT_CONFIG_INVALID", "项目列表格式无效，未使用空列表替代。")
    return result


def revision() -> str:
    return digest({"connection": read_json(active_path()), "active": project_active_settings(), "registry": registry()})


@contextmanager
def _legacy_connection_lock():
    """Serialize commits with settings windows from already-running v1 tools."""
    path = active_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.seek(0, 2) == 0:
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
        except OSError as error:
            raise ProjectError("PROJECT_CONFIG_BUSY", "另一处正在更新连接，请稍后重试。") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _metadata(connection, pointer):
    return {"schema_version": 1, "recorded_connection_revision": connection["revision"],
            "connection": connection_identity(connection), "project": pointer}


def publish_active(profile: dict, pointer: dict, *, expected_revision: str | None = None):
    """Commit strict v1 connection bytes; preserve immutable project profiles."""
    staged = []
    committed = False
    try:
        with operation_gate(), _legacy_connection_lock():
            if expected_revision is not None:
                _check_revision(expected_revision)
            if (pointer.get("project_id") != profile["project_id"]
                    or pointer.get("profile_revision") != profile["revision"]):
                raise ProjectError("PROJECT_CONFIG_INVALID", "项目指针与已保存版本不一致。")
            base = _base_config("write")
            account = profile["accounts"]["write"]
            if any(account.get(key, default) != base.get(key, default) for key, default in
                   (("account_mode", "dedicated"), ("expected_grants_sha256", ""))):
                raise ProjectError("PROJECT_WRITE_POLICY_INCOMPATIBLE",
                                   "此项目的 Write 权限策略与旧工具配置不同，未切换；请先统一权限策略。")
            _secure_keyring()
            connection = {"schema_version": 1, "revision": secrets.token_hex(16),
                          **profile["database"], "accounts": {}}
            for role in ("look", "write"):
                original = profile["accounts"][role]
                password = keyring.get_password(original["credential_target"], original["user"])
                if not password:
                    raise ProjectError("PROJECT_CREDENTIALS_REQUIRED", "缺少数据库凭据，请在本机窗口输入。", {"role": role})
                target = "GxpLowcode.Connection." + connection["revision"] + "." + role
                keyring.set_password(target, original["user"], password)
                staged.append((target, original["user"]))
                connection["accounts"][role] = {"user": original["user"], "credential_target": target}
            # Metadata is staged first. Until the atomic v1 replace, its revision
            # does not match and new readers keep using the previous connection.
            previous_metadata = read_json(project_metadata_path())
            atomic_json(project_metadata_path(), _metadata(connection, pointer))
            try:
                atomic_json(active_path(), connection)
            except Exception:
                # Keep the previously active project identifiable if the final
                # connection replace fails after staging the new metadata.
                atomic_json(project_metadata_path(), previous_metadata or {})
                raise
            committed = True
        return connection
    finally:
        if not committed:
            for target, user in staged:
                try:
                    keyring.delete_password(target, user)
                except Exception:
                    pass


def publish_metadata(pointer: dict):
    """Refresh project metadata without changing the old tools' connection."""
    with operation_gate(), _legacy_connection_lock():
        active = project_active_settings() or {}
        if active.get("schema_version") != CONTRACT_VERSION or any(
                pointer.get(key) != active.get(key) for key in ("project_id", "profile_revision", "activation_id")):
            raise ProjectError("PROJECT_CHANGED", "项目已变化，未更新刷新代次。")
        connection = read_json(active_path()) or {}
        if connection.get("schema_version") == 1:
            atomic_json(project_metadata_path(), _metadata(connection, pointer))
        else:
            # Migrate a pre-existing v2 pointer through the same compatibility
            # commit rather than leaving another v2 file for old readers.
            profile = load_profile(pointer["project_id"], pointer["profile_revision"])
            expected = revision()
            # The legacy lock is non-reentrant; release before the full commit.
    if connection.get("schema_version") != 1:
        publish_active(profile, pointer, expected_revision=expected)


def _check_revision(expected: str):
    if not expected or expected != revision():
        raise ProjectError("PROJECT_CONFIG_CONFLICT", "项目配置已变化，请重新读取后再提交。")


def _text(value, name, *, optional=False):
    if optional and not value:
        return ""
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ProjectError("PROJECT_FIELDS_INVALID", f"{name} 须为非空文本。")
    return value.strip()


def reject_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(r"(?i)(?:password|passwd|secret|connectionstring|connection_string|cookie|authorization|token|api_key|apikey)$", str(key)):
                raise ProjectError("PROJECT_PRIVATE_FIELD", "凭据只能在本机窗口输入，不能放入项目工具参数。")
            reject_secrets(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_secrets(item)


def _secure_keyring():
    backend = keyring.get_keyring()
    module = type(backend).__module__.lower()
    if backend.priority <= 0 or not any(name in module for name in ("windows", "macos", "secretservice", "libsecret")):
        raise ProjectError("PROJECT_KEYRING_UNAVAILABLE", "操作系统安全凭据库不可用。")


def _legacy_config_root(*, resolve=False) -> Path:
    override = os.environ.get("GXP_LOWCODE_CONFIG_ROOT")
    if override:
        path = Path(override).expanduser()
        return path.resolve() if resolve else path
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()


def _legacy_cpm_config_path() -> Path:
    override = os.environ.get("GXP_LOWCODE_CPM_CONFIG")
    return Path(override) if override else _legacy_config_root() / "GxpLowcodeReadonly/cpm.json"


def _legacy_schema_config_path() -> Path:
    override = os.environ.get("GXP_LOWCODE_SCHEMA_CONFIG")
    return Path(override).expanduser().resolve() if override else _legacy_config_root(resolve=True) / "GxpLowcodeReadonly/schema.json"


def _legacy_schema_runtime_root() -> Path:
    override = os.environ.get("GXP_LOWCODE_RUNTIME_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share").expanduser()
    return base / "GxpLowcodeReadonly"


def _legacy_form_config_path() -> Path:
    override = os.environ.get("GXP_FORM_EDITOR_CONFIG")
    return Path(override).expanduser().resolve() if override else Path(os.environ.get("APPDATA") or Path.home() / ".config") / "GxpFormEditor/form-config.json"


def legacy_platform_defaults() -> dict:
    """Read existing non-secret platform metadata, never password or token files."""
    cpm = read_json(_legacy_cpm_config_path()) or {}
    reject_secrets(cpm)
    form = read_json(_legacy_form_config_path()) or {}
    reject_secrets(form)

    def same_url(left, right):
        if not left or not right:
            return False
        try:
            a, b = urlsplit(str(left)), urlsplit(str(right))
            def key(p):
                return p.scheme.lower(), (p.hostname or "").lower(), p.port or (443 if p.scheme == "https" else 80), p.path.rstrip("/")
            return key(a) == key(b)
        except ValueError:
            return False

    url = str(cpm.get("platform_url") or form.get("base_url") or "").rstrip("/")
    native = {}
    snapshot = cpm.get("snapshot_dir")
    if snapshot:
        candidate = read_json(Path(snapshot) / ".cpm" / "config.json") or {}
        reject_secrets(candidate)
        if same_url(candidate.get("url"), url):
            native = candidate
    form_matches = same_url(form.get("base_url"), url)
    form_config = {key: value for key, value in form.items() if key in {
        "environment", "platform", "api_prefix", "contract_path", "auth_header", "company_id", "org_identity_id"
    }} if form_matches else {}
    if form_config.get("contract_path"):
        path = Path(form_config["contract_path"])
        try:
            if path.name.lower() in {"token.json", "credentials.json"}:
                raise ProjectError("PROJECT_CONTRACT_INVALID", "表单协议路径无效。")
            contract = read_json(path) or {}
            reject_secrets(contract)
            if not same_url(contract.get("base_url"), url):
                form_config.pop("contract_path", None)
        except (ProjectError, OSError):
            form_config.pop("contract_path", None)
    if native.get("apiPrefix") and not form_config.get("api_prefix"):
        form_config["api_prefix"] = native["apiPrefix"]
    binding = {"platform_url": url, "base_url": url,
               "app_id": cpm.get("app_id") or native.get("appId") or (form.get("app_id") if form_matches else "") or "",
               "site_out_id": cpm.get("site_out_id") or native.get("siteOutId") or (form.get("site_out_id") if form_matches else "") or "",
               "account": cpm.get("account") or (form.get("platform_account") if form_matches else "") or "",
               "form_config": form_config}
    if cpm.get("account"):
        binding["credential_target"] = cpm.get("credential_target") or "Codex.GxpLowcodeReadonly.Cpm"
    version = {key: cpm[key] for key in ("node_path", "cli_path") if cpm.get(key)}
    return {"platform_binding": binding, "platform_version": {"cpm": version} if version else {},
            "configured": bool(url and binding["app_id"] and binding["site_out_id"])}


def _normalize(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ProjectError("PROJECT_FIELDS_INVALID", "项目配置必须是对象。")
    reject_secrets(value)
    allowed = {"name", "environment", "database", "accounts", "platform_binding", "platform_version", "refresh", "business_scope_id", "reuse_credentials_from"}
    if set(value) - allowed:
        raise ProjectError("PROJECT_FIELDS_INVALID", "项目配置包含不支持的字段。")
    db = value.get("database") or {}
    if not isinstance(db, dict) or set(db) - {"host", "port", "database"}:
        raise ProjectError("PROJECT_FIELDS_INVALID", "数据库配置仅允许地址、端口和库名。")
    db = {"host": _text(db.get("host"), "地址"), "port": db.get("port", 3306), "database": _text(db.get("database"), "库名")}
    if "://" in db["host"] or any(c in db["host"] for c in "/\\@"):
        raise ProjectError("PROJECT_FIELDS_INVALID", "数据库地址不能是 URL 或连接串。")
    scope = database_scope(db)
    reuse_source = value.get("reuse_credentials_from", "")
    if reuse_source and (not isinstance(reuse_source, str) or not re.fullmatch(r"[a-f0-9]{64}", reuse_source)):
        raise ProjectError("PROJECT_CREDENTIAL_SOURCE_INVALID", "请选择已保存的项目作为凭据来源。")
    accounts = value.get("accounts") or {}
    if not isinstance(accounts, dict) or set(accounts) != {"look", "write"}:
        raise ProjectError("PROJECT_FIELDS_INVALID", "请配置 Look 和 Write 两个账号。")
    cleaned = {}
    for role in ("look", "write"):
        account = accounts[role]
        if not isinstance(account, dict) or set(account) - {"user", "credential_target", "account_mode", "expected_grants_sha256"}:
            raise ProjectError("PROJECT_FIELDS_INVALID", "账号字段无效。")
        cleaned[role] = {"user": _text(account.get("user"), role + "账号")}
        if account.get("credential_target"):
            cleaned[role]["credential_target"] = _text(account["credential_target"], "凭据引用")
        if role == "write":
            mode = account.get("account_mode", "dedicated")
            if mode not in {"dedicated", "shared_restricted", "shared_unrestricted"}:
                raise ProjectError("PROJECT_FIELDS_INVALID", "Write 账户模式无效。")
            grants = account.get("expected_grants_sha256", "")
            if grants and not re.fullmatch(r"[a-f0-9]{64}", grants):
                raise ProjectError("PROJECT_FIELDS_INVALID", "Write 权限指纹无效。")
            cleaned[role].update(account_mode=mode, expected_grants_sha256=grants)
    platform = value.get("platform_binding") or {}
    if not isinstance(platform, dict) or set(platform) - {"platform_url", "base_url", "app_id", "site_out_id", "account", "credential_target", "form_config", "api_prefix"}:
        raise ProjectError("PROJECT_FIELDS_INVALID", "平台配置字段无效。")
    url = _text(platform.get("platform_url") or platform.get("base_url"), "平台地址", optional=True).rstrip("/")
    parsed = urlsplit(url)
    if url and (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ProjectError("PROJECT_FIELDS_INVALID", "平台地址必须是不含凭据和查询参数的 HTTP(S) 地址。")
    binding = {**platform, "platform_url": url, "base_url": url,
               "app_id": _text(platform.get("app_id"), "应用标识", optional=True),
               "site_out_id": _text(platform.get("site_out_id"), "站点标识", optional=True),
               "account": _text(platform.get("account"), "平台账号", optional=True)}
    env = value.get("environment", "development")
    if env not in {"development", "test", "staging", "production", "unknown"}:
        raise ProjectError("PROJECT_FIELDS_INVALID", "环境标签无效。")
    form = copy.deepcopy(binding.get("form_config") or {})
    if not isinstance(form, dict):
        raise ProjectError("PROJECT_FIELDS_INVALID", "表单平台配置无效。")
    form.update(base_url=url, environment=env)
    form.update(platform_account=binding["account"], app_id=binding["app_id"], site_out_id=binding["site_out_id"])
    form.setdefault("platform", "web")
    form.setdefault("auth_header", "Cookie")
    form.setdefault("credential_target", "GxpLowcode.Project.Form." + scope)
    for key in ("company_id", "org_identity_id"):
        form[key] = _text(form.get(key), key, optional=True)
    binding["form_config"] = form
    refresh = value.get("refresh") or {}
    if not isinstance(refresh, dict) or set(refresh) - {"timeout_seconds", "http_concurrency", "database_concurrency"}:
        raise ProjectError("PROJECT_FIELDS_INVALID", "刷新配置字段无效。")
    normalized_refresh = {}
    for key, default, high in (("timeout_seconds", 900, 7200), ("http_concurrency", 4, 16), ("database_concurrency", 2, 8)):
        number = refresh.get(key, default)
        if type(number) is not int or not 1 <= number <= high:
            raise ProjectError("PROJECT_FIELDS_INVALID", "刷新预算无效。")
        normalized_refresh[key] = number
    platform_version = value.get("platform_version") or {}
    if not isinstance(platform_version, dict):
        raise ProjectError("PROJECT_FIELDS_INVALID", "平台版本配置无效。")
    result = {"schema_version": CONTRACT_VERSION, "project_id": scope,
            "name": _text(value.get("name") or db["database"], "项目名称"),
            "database": db, "accounts": cleaned, "environment": env,
            "business_scope_id": _business_scope(scope), "platform_binding": binding,
            "platform_version": platform_version, "refresh": normalized_refresh}
    if reuse_source:
        result["reuse_credentials_from"] = reuse_source
    return result


def _credential_source(project_id: str, index: dict) -> dict | None:
    """Resolve an explicitly selected source without retrieving any password."""
    if not project_id:
        return None
    entry = index["projects"].get(project_id)
    if entry:
        return load_profile(project_id, entry["revision"])
    # The first profile may be copied from the currently configured legacy
    # connection. Only that exact database is eligible, never another profile
    # discovered by a matching account name.
    context = get_context(role="look")
    if context is None or context.database_scope != project_id:
        raise ProjectError("PROJECT_CREDENTIAL_SOURCE_NOT_FOUND", "凭据来源项目已不存在，请重新选择。")
    accounts = {}
    for role in ("look", "write"):
        account = context.database_configs.get(role) or _base_config(role)
        if isinstance(account, dict) and account.get("user"):
            accounts[role] = {"user": account.get("user", ""), "credential_target": account.get("credential_target") or
                              ("Codex.GxpLowcodeReadonly" if role == "look" else "Codex.GxpLowcodeEditor.Database")}
    return {"accounts": accounts, "platform_binding": dict(context.platform_binding) or legacy_platform_defaults()["platform_binding"]}


def _public_profile(profile):
    result = copy.deepcopy(profile)
    # Credential references contain no secrets. Native request templates stay local.
    result["platform_binding"].pop("form_config", None)
    return result


def save_current_project(expected_revision: str, name: str = "") -> dict:
    """Register the effective connection without reconnecting or activating it."""
    context = get_context(required=True, role="look")
    if context.managed:
        current = load_profile(context.project_id, context.profile_revision)
        profile = {key: current[key] for key in ("name", "environment", "database", "accounts", "platform_binding", "platform_version", "refresh")}
        if name:
            profile["name"] = name
    else:
        primary = context.database_configs["look"]
        database = {key: primary[key] for key in ("host", "port", "database")}
        accounts = {}
        for role in ("look", "write"):
            original = {**_base_config(role), **context.database_configs.get(role, {})}
            allowed = ("user", "credential_target", "account_mode", "expected_grants_sha256") if role == "write" else ("user", "credential_target")
            accounts[role] = {key: original[key] for key in allowed if key in original}
        defaults = legacy_platform_defaults()
        profile = {"name": name or "原配置 · " + database["host"] + ":" + str(database["port"]) + "/" + database["database"],
                   "environment": context.environment, "database": database, "accounts": accounts,
                   "platform_binding": defaults["platform_binding"], "platform_version": defaults["platform_version"],
                   "reuse_credentials_from": context.project_id}
    result = save_project(profile, expected_revision)
    return {**result, "saved_current_connection": True,
            "message": "当前连接已保存到项目列表，原配置、账号密码及当前数据库连接均保留。"}


def save_project(value: dict, expected_revision: str, *, passwords: dict | None = None) -> dict:
    profile = _normalize(value)
    staged = []
    try:
        with operation_gate():
            _check_revision(expected_revision)
            index = registry()
            previous = index["projects"].get(profile["project_id"])
            old = load_profile(profile["project_id"], previous["revision"]) if previous else None
            source = _credential_source(profile.get("reuse_credentials_from", ""), index)
            if old:
                profile["business_scope_id"] = old["business_scope_id"]
            for role in ("look", "write"):
                account = profile["accounts"][role]
                supplied = (passwords or {}).get(role)
                if supplied is not None:
                    if not isinstance(supplied, str) or not supplied:
                        raise ProjectError("PROJECT_CREDENTIALS_REQUIRED", "密码不能为空。")
                    _secure_keyring()
                    target = "GxpLowcode.Connection." + secrets.token_hex(16) + "." + role
                    keyring.set_password(target, account["user"], supplied)
                    staged.append((target, account["user"]))
                    account["credential_target"] = target
                elif not account.get("credential_target") and old and old["accounts"][role]["user"] == account["user"]:
                    account["credential_target"] = old["accounts"][role].get("credential_target", "")
                elif not account.get("credential_target") and source:
                    inherited = source.get("accounts", {}).get(role) or {}
                    if inherited.get("user") != account["user"] or not inherited.get("credential_target"):
                        raise ProjectError("PROJECT_CREDENTIAL_USER_MISMATCH", "复用凭据时用户名须与来源项目一致；更换账号请在本机窗口填写新密码。", {"role": role})
                    account["credential_target"] = inherited["credential_target"]
                elif not account.get("credential_target") and old is None:
                    legacy = _base_config(role)
                    try:
                        same_endpoint = database_scope(legacy) == profile["project_id"]
                    except ProjectError:
                        same_endpoint = False
                    if same_endpoint and legacy.get("user") == account["user"]:
                        account["credential_target"] = legacy.get("credential_target") or ("Codex.GxpLowcodeReadonly" if role == "look" else "Codex.GxpLowcodeEditor.Database")
                account.setdefault("credential_target", "GxpLowcode.Project." + profile["project_id"] + "." + role)
            platform_password = (passwords or {}).get("platform")
            if platform_password is not None:
                _secure_keyring()
                target = "GxpLowcode.Platform." + secrets.token_hex(16)
                account = profile["platform_binding"]["account"]
                if not account or not isinstance(platform_password, str) or not platform_password:
                    raise ProjectError("PROJECT_CREDENTIALS_REQUIRED", "请填写平台账号和密码。")
                keyring.set_password(target, account, platform_password)
                staged.append((target, account))
                profile["platform_binding"]["credential_target"] = target
            elif not profile["platform_binding"].get("credential_target") and old:
                if all(profile["platform_binding"].get(k) == old["platform_binding"].get(k) for k in ("platform_url", "account")):
                    profile["platform_binding"]["credential_target"] = old["platform_binding"].get("credential_target", "")
            if not profile["platform_binding"].get("credential_target") and source:
                inherited = source.get("platform_binding") or {}
                if inherited.get("account") and inherited["account"] == profile["platform_binding"].get("account"):
                    profile["platform_binding"]["credential_target"] = inherited.get("credential_target", "")
            if not profile["platform_binding"].get("credential_target"):
                profile["platform_binding"]["credential_target"] = "GxpLowcode.Project.Platform." + profile["project_id"]
            profile["revision"] = digest(profile)
            destination = config_root() / "profiles" / profile["project_id"] / (profile["revision"] + ".json")
            if not destination.exists():
                atomic_json(destination, profile)
            index["projects"][profile["project_id"]] = {"name": profile["name"], "revision": profile["revision"]}
            atomic_json(config_root() / "projects.json", index)
            staged.clear()
            return {"ok": True, "saved": True, "activated": False, "project_id": profile["project_id"],
                    "profile_revision": profile["revision"], "revision": revision(),
                    "profile": _public_profile(profile)}
    finally:
        for target, user in staged:
            try:
                keyring.delete_password(target, user)
            except Exception:
                pass


def _state_paths():
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/share")
    action = Path(os.environ["GXP_LOWCODE_EDITOR_STATE"]) if os.environ.get("GXP_LOWCODE_EDITOR_STATE") else base / "GxpLowcodeEditor/state/editor.sqlite3"
    form_root = Path(os.environ["GXP_FORM_CONFIG_ROOT"]) if os.environ.get("GXP_FORM_CONFIG_ROOT") else base / "GxpLowcodeEditor/form-config"
    return action, form_root / "state.sqlite3"


def active_sessions() -> list[dict]:
    from .local_sessions import active_sessions as list_local
    return list_local()


def probe_database(profile: dict):
    import pymysql
    for role in ("look", "write"):
        account = profile["accounts"][role]
        password = keyring.get_password(account["credential_target"], account["user"])
        if not password:
            raise ProjectError("PROJECT_CREDENTIALS_REQUIRED", "缺少数据库凭据，请在本机窗口输入。", {"role": role})
        try:
            connection = pymysql.connect(**profile["database"], user=account["user"], password=password,
                                         connect_timeout=5, read_timeout=5, write_timeout=5, autocommit=False)
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET SESSION TRANSACTION READ ONLY")
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                connection.rollback()
            finally:
                connection.close()
        except Exception as exc:
            raise ProjectError("PROJECT_CONNECTION_FAILED", "目标数据库连接测试失败，原项目保留。", {"role": role}) from exc


def probe_platform(profile: dict) -> dict:
    """Use the existing adapter's authenticated read contract, never invent an endpoint."""
    binding = profile["platform_binding"]
    form = binding.get("form_config") or {}
    if not all(binding.get(key) for key in ("platform_url", "app_id", "site_out_id")):
        return {"status": "unavailable", "reason": "platform_settings_missing"}
    try:
        from urllib.request import Request, build_opener
        from .platform_auth import NoRedirect, credential as get_credential, login_platform, prefix
        auth_header = form.get("auth_header") or "Cookie"
        credential = get_credential(profile)
        if not credential:
            password = keyring.get_password(binding["credential_target"], binding.get("account", ""))
            if password:
                login_platform(profile, password)
                credential = get_credential(profile)
        if not credential:
            return {"status": "unavailable", "reason": "platform_login_required"}
        request = Request(binding["platform_url"] + prefix(profile) + "/api/app_info", headers={auth_header: credential})
        with build_opener(NoRedirect).open(request, timeout=5) as response:
            info = json.loads(response.read(1024 * 1024)).get("data") or {}
        actual_app, actual_site = info.get("appId", info.get("AppId")), info.get("siteOutId", info.get("SiteOutId"))
        if actual_app is None or actual_site is None:
            return {"status": "unavailable", "reason": "platform_identity_unavailable"}
        if (actual_app, actual_site) != (binding["app_id"], binding["site_out_id"]):
            raise ProjectError("PROJECT_PLATFORM_MISMATCH", "平台返回的应用或站点与项目配置不同，原项目保留。")
        return {"status": "ready"}
    except ProjectError:
        raise
    except Exception:
        return {"status": "unavailable", "reason": "platform_probe_failed"}


def _backup_legacy():
    from datetime import datetime, timezone
    destination = config_root() / "migration" / "legacy"
    if (destination / "manifest.json").exists():
        return
    destination.mkdir(parents=True, exist_ok=True)
    paths = [active_path(), *_state_paths(), base_config_path("look"), base_config_path("write")]
    paths.extend([_legacy_cpm_config_path(), _legacy_schema_config_path(), _legacy_schema_runtime_root() / "business-graph.sqlite3",
                  _legacy_schema_runtime_root() / "business-logic-graph.json", _legacy_schema_runtime_root() / "relation-policy-local.json",
                  _legacy_form_config_path()])
    entries = []
    for index, path in enumerate(paths):
        if not path.is_file():
            continue
        target = destination / (str(index) + "-" + path.name)
        if path.suffix == ".sqlite3":
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(target)) as backup:
                source.backup(backup)
        else:
            import shutil
            shutil.copy2(path, target)
        entries.append({"source": str(path), "backup": target.name, "ownership": "unverified"})
    atomic_json(destination / "manifest.json", {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "entries": entries, "automatic_claim": False})


def switch_project(project_id: str, expected_revision: str, request_id: str = "") -> dict:
    from .refresh import initialize_refresh, launch_refresh, refresh_status
    if request_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
        raise ProjectError("PROJECT_REQUEST_INVALID", "切换请求标识无效。")
    receipt_path = config_root() / "switches" / (digest(request_id) + ".json") if request_id else None
    receipt = read_json(receipt_path) if receipt_path is not None else None
    if receipt is not None:
        if receipt.get("project_id") != project_id:
            raise ProjectError("PROJECT_REQUEST_CONFLICT", "同一切换请求不能用于另一个项目。")
        current = project_active_settings() or {}
        return {"ok": True, "changed": False, "applied": True, "replayed": True,
                "project_id": receipt["project_id"], "active_project_id": current.get("project_id"),
                "revision": revision(), "job_id": receipt["job_id"], "refresh": refresh_status(receipt["job_id"])}
    active = project_active_settings() or {}
    if request_id and active.get("switch_id") == request_id:
        if active.get("project_id") != project_id:
            raise ProjectError("PROJECT_REQUEST_CONFLICT", "同一切换请求不能用于另一个项目。")
        return {"ok": True, "changed": True, "replayed": True, "project_id": active["project_id"], "revision": revision(),
                "refresh": refresh_status(active["refresh_generation"])}
    _check_revision(expected_revision)
    entry = registry()["projects"].get(project_id)
    if not entry:
        raise ProjectError("PROJECT_NOT_FOUND", "未找到已保存的项目。")
    profile = load_profile(project_id, entry["revision"])
    if active_sessions():
        raise ProjectError("PROJECT_ACTIVE_SESSIONS", "仍有动作或表单流程未结束，原项目保留。", {"active_edit_sessions": active_sessions()})
    probe_database(profile)
    platform = probe_platform(profile)
    with operation_gate():
        _check_revision(expected_revision)
        sessions = active_sessions()
        if sessions:
            raise ProjectError("PROJECT_ACTIVE_SESSIONS", "测试期间出现活动流程，原项目保留。", {"active_edit_sessions": sessions})
        _backup_legacy()
        activation, generation = secrets.token_hex(16), secrets.token_hex(16)
        ctx = ProjectContext(project_id, project_id, profile["business_scope_id"], profile["environment"],
            profile["revision"], activation, generation,
            {role: {**profile["database"], **profile["accounts"][role], "environment": profile["environment"]} for role in ("look", "write")},
            profile["platform_binding"], {**profile["platform_version"], "refresh": profile["refresh"]}, True, profile["name"])
        initialize_refresh(ctx, platform_probe=platform)
        pointer = {"schema_version": CONTRACT_VERSION, "project_id": project_id,
                   "profile_revision": profile["revision"], "activation_id": activation,
                   "refresh_generation": generation, "revision": secrets.token_hex(16),
                   "switch_id": request_id or secrets.token_hex(16), "refresh_state": "pending"}
        publish_active(profile, pointer, expected_revision=expected_revision)
        if receipt_path is not None:
            try:
                atomic_json(receipt_path, {"project_id": project_id, "profile_revision": profile["revision"],
                                          "activation_id": activation, "job_id": generation})
            except OSError:
                # The single pointer is already committed. Its switch_id still
                # makes an immediate response retry idempotent; never undo it.
                pass
    launched = launch_refresh(ctx)
    return {"ok": True, "changed": True, "project_id": project_id, "project_context": ctx.binding(),
            "revision": revision(), "job_id": generation, "refresh": launched,
            "message": "已切换目标项目并启动全量刷新；缺项不会使用其他项目资料补齐。"}


def settings_status() -> dict:
    ctx = get_context(role="look")
    active = project_active_settings() or {}
    if ctx:
        primary = ctx.database_configs.get("look") or {}
        settings = {key: primary.get(key, "") for key in ("host", "port", "database")}
        accounts = {}
        for role in ("look", "write"):
            accounts[role] = dict(ctx.database_configs.get(role, {})) if ctx.managed else {**_base_config(role), **ctx.database_configs.get(role, {})}
            settings[role + "_user"] = accounts[role].get("user", "")
        settings["account_mode"] = accounts["write"].get("account_mode", "dedicated")
        settings["expected_grants_sha256"] = accounts["write"].get("expected_grants_sha256", "")
    else:
        settings = {"host": "", "port": 3306, "database": "", "look_user": "", "write_user": ""}
    from .refresh import refresh_status
    refresh = refresh_status(active.get("refresh_generation", "")) if active.get("schema_version") == CONTRACT_VERSION else None
    defaults = {"platform_binding": dict(ctx.platform_binding), "platform_version": dict(ctx.platform_version), "configured": True} if ctx and ctx.managed else legacy_platform_defaults()
    return {"ok": True, "configured": bool(ctx), "project_mode": bool(ctx and ctx.managed),
            "revision": revision(), "settings": settings, "config_path": str(active_path()),
            "project_context": ctx.binding() if ctx else None, "active_project_id": ctx.project_id if ctx else None,
            "active_project_name": ctx.name if ctx else "", "refresh": refresh,
            "current_connection_saved": bool(ctx and ctx.project_id in registry()["projects"]),
            "platform_defaults": defaults,
            "active_edit_sessions": active_sessions(), "projects": list_projects()["projects"]}


def list_projects() -> dict:
    active = project_active_settings() or {}
    result = []
    for project_id, entry in registry()["projects"].items():
        profile = load_profile(project_id, entry["revision"])
        summary = _public_profile(profile)
        summary["active"] = active.get("project_id") == project_id
        result.append(summary)
    return {"ok": True, "projects": result, "revision": revision()}


def manage(operation: str, *, project_id: str = "", profile: dict | None = None, expected_revision: str = "", job_id: str = "", request_id: str = "", retry_failed: bool = False) -> dict:
    try:
        if operation == "status":
            return settings_status()
        if operation == "list_projects":
            return list_projects()
        if operation == "save_project":
            return save_project(profile or {}, expected_revision)
        if operation == "save_current_project":
            if profile and set(profile) - {"name"}:
                raise ProjectError("PROJECT_FIELDS_INVALID", "保存当前连接仅支持可选项目名称。")
            return save_current_project(expected_revision, (profile or {}).get("name", ""))
        if operation == "switch_project":
            return switch_project(project_id, expected_revision, request_id)
        if operation == "refresh_status":
            from .refresh import refresh_status
            return {"ok": True, **refresh_status(job_id)}
        if operation == "refresh_project":
            from .refresh import refresh_project
            return refresh_project(expected_revision, retry_failed=retry_failed)
        raise ProjectError("PROJECT_OPERATION_INVALID", "不支持的项目设置操作。")
    except ProjectError as exc:
        return {"ok": False, "changed": False, "error_code": exc.code, "error": str(exc), "error_details": exc.details}
    except Exception:
        return {"ok": False, "changed": False, "error_code": "PROJECT_SETTINGS_FAILED", "error": "项目设置未完成，请检查本机配置及系统凭据库。"}
