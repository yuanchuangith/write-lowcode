"""One immutable context per operation; no credential values enter this module."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONTRACT_VERSION = 2
_HASH = re.compile(r"^[a-f0-9]{64}$")
_ID = re.compile(r"^[a-f0-9]{32}$")
_PINNED = ContextVar("gxp_project_context", default=None)
_PIN_SET = ContextVar("gxp_project_context_pinned", default=False)
_GATE = ContextVar("gxp_project_operation_gate", default=False)
_THREAD_GATE = threading.RLock()


class ProjectError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.details = code, details or {}


class FrozenDict(dict):
    def _immutable(self, *args, **kwargs):
        raise TypeError("ProjectContext is immutable")
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable
    def __deepcopy__(self, memo):
        return self


def _freeze(value):
    if isinstance(value, dict):
        return FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def database_scope(config: Any) -> str:
    def value(key):
        return config.get(key) if isinstance(config, dict) else getattr(config, key, None)
    host, database, port = value("host"), value("database"), value("port")
    if not isinstance(host, str) or not host.strip() or not isinstance(database, str) or not database.strip():
        raise ProjectError("PROJECT_DATABASE_INVALID", "数据库身份不完整。")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ProjectError("PROJECT_DATABASE_INVALID", "数据库端口无效。")
    return digest({"host": host.strip().lower(), "port": port, "database": database.strip()})


def _user_config_home() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def active_path() -> Path:
    value = os.environ.get("GXP_LOWCODE_SHARED_CONFIG")
    return Path(value).expanduser().resolve() if value else _user_config_home() / "GxpLowcode/connection.json"


def project_metadata_path() -> Path:
    return active_path().with_suffix(".project.json")


def connection_identity(connection: dict) -> dict:
    return {key: connection.get(key) for key in ("host", "port", "database", "accounts")}


def project_active_settings() -> dict | None:
    """Resolve project metadata only when it describes the committed v1 file."""
    connection = read_json(active_path())
    if connection is None or connection.get("schema_version") != 1:
        return connection  # Existing v2 pointers remain readable.
    try:
        metadata = read_json(project_metadata_path())
    except ProjectError:
        return connection
    if (not metadata or metadata.get("schema_version") != 1
            or metadata.get("recorded_connection_revision") != connection.get("revision")
            or metadata.get("connection") != connection_identity(connection)):
        return connection
    pointer = metadata.get("project")
    if not isinstance(pointer, dict) or pointer.get("schema_version") != CONTRACT_VERSION:
        return connection
    profile = load_profile(pointer.get("project_id", ""), pointer.get("profile_revision", ""))
    if (profile["database"] != {key: connection.get(key) for key in ("host", "port", "database")}
            or any(profile["accounts"][role]["user"] != connection.get("accounts", {}).get(role, {}).get("user")
                   for role in ("look", "write"))):
        return connection
    return pointer


def config_root() -> Path:
    value = os.environ.get("GXP_PROJECT_CONFIG_ROOT")
    return Path(value).expanduser().resolve() if value else active_path().with_suffix(".projects")


def runtime_root() -> Path:
    for name in ("GXP_PROJECT_RUNTIME_ROOT", "GXP_LOWCODE_RUNTIME_ROOT"):
        if os.environ.get(name):
            root = Path(os.environ[name]).expanduser().resolve()
            return root if name == "GXP_PROJECT_RUNTIME_ROOT" else root / "projects"
    if os.environ.get("GXP_LOWCODE_SHARED_CONFIG"):
        return active_path().parent / ("." + active_path().stem + "-project-runtime")
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/share")
    return base / "GxpLowcode/projects"


def read_json(path: Path, *, max_bytes: int = 2 * 1024 * 1024):
    if not path.exists():
        return None
    try:
        if path.stat().st_size > max_bytes:
            raise ValueError("oversized")
        result = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(result, dict):
            raise ValueError("object required")
        return result
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProjectError("PROJECT_CONFIG_INVALID", "项目配置或状态无法读取；未使用其他连接作为替代。") from exc


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".project-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@dataclass(frozen=True)
class ProjectContext:
    project_id: str
    database_scope: str
    business_scope_id: str
    environment: str = "development"
    profile_revision: str = ""
    activation_id: str = ""
    refresh_generation: str = "legacy"
    database_configs: dict = field(default_factory=dict)
    platform_binding: dict = field(default_factory=dict)
    platform_version: dict = field(default_factory=dict)
    managed: bool = False
    name: str = ""

    def __post_init__(self):
        if not _HASH.fullmatch(self.database_scope) or self.project_id != self.database_scope:
            raise ProjectError("PROJECT_CONTEXT_INVALID", "项目数据库身份无效。")
        if self.managed and (not _ID.fullmatch(self.activation_id) or not _ID.fullmatch(self.refresh_generation)):
            raise ProjectError("PROJECT_CONTEXT_INVALID", "项目激活或刷新代次无效。")
        for key in ("database_configs", "platform_binding", "platform_version"):
            object.__setattr__(self, key, _freeze(copy.deepcopy(getattr(self, key))))

    def binding(self) -> dict:
        return {key: getattr(self, key) for key in
                ("project_id", "database_scope", "profile_revision", "activation_id")}

    def persistent_root(self, kind: str = "") -> Path:
        if kind and not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", kind):
            raise ProjectError("PROJECT_PATH_INVALID", "项目资料类型无效。")
        root = runtime_root() / self.database_scope
        return root / kind if kind else root

    def snapshot_root(self, kind: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", kind):
            raise ProjectError("PROJECT_PATH_INVALID", "项目快照类型无效。")
        return self.persistent_root() / "generations" / self.refresh_generation / kind

    def to_dict(self) -> dict:
        return {key: _plain(getattr(self, key)) for key in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: dict):
        return cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})


def base_config_path(role: str) -> Path:
    variable = "GXP_LOWCODE_CONFIG" if role == "look" else "GXP_LOWCODE_EDITOR_CONFIG"
    default_base = _user_config_home() if role == "look" or os.name == "nt" else Path(os.environ.get("APPDATA") or Path.home() / ".config")
    return Path(os.environ[variable]).expanduser().resolve() if os.environ.get(variable) else default_base / ("GxpLowcodeReadonly" if role == "look" else "GxpLowcodeEditor") / "database.json"


def _base_config(role: str) -> dict:
    result = read_json(base_config_path(role)) or {}
    return result.get("database", {}) if role == "write" else result


def _business_scope(scope: str) -> str:
    path = Path(os.environ["GXP_LOWCODE_SCHEMA_CONFIG"]) if os.environ.get("GXP_LOWCODE_SCHEMA_CONFIG") else _user_config_home() / "GxpLowcodeReadonly/schema.json"
    base = str((read_json(path) or {}).get("policy_scope_id") or "gxp-development")
    base = re.sub(r"\|database:[0-9a-f]{64}$", "", base)
    return base + "|database:" + scope


def load_profile(project_id: str, revision: str) -> dict:
    if not _HASH.fullmatch(project_id or "") or not _HASH.fullmatch(revision or ""):
        raise ProjectError("PROJECT_CONFIG_INVALID", "项目配置版本无效。")
    profile = read_json(config_root() / "profiles" / project_id / (revision + ".json"))
    if profile is None or digest({key: value for key, value in profile.items() if key != "revision"}) != revision:
        raise ProjectError("PROJECT_CONFIG_INVALID", "项目配置缺失或内容指纹不匹配。")
    if profile.get("project_id") != project_id or database_scope(profile.get("database", {})) != project_id:
        raise ProjectError("PROJECT_CONFIG_INVALID", "项目数据库身份不匹配。")
    if profile.get("schema_version") != CONTRACT_VERSION or not isinstance(profile.get("accounts"), dict) or set(profile["accounts"]) != {"look", "write"}:
        raise ProjectError("PROJECT_CONFIG_INVALID", "项目配置协议或账号无效。")
    if set(profile["database"]) != {"host", "port", "database"}:
        raise ProjectError("PROJECT_CONFIG_INVALID", "数据库配置字段无效。")
    for role, account in profile["accounts"].items():
        allowed = {"user", "credential_target"} | ({"account_mode", "expected_grants_sha256"} if role == "write" else set())
        if not isinstance(account, dict) or set(account) - allowed or not all(isinstance(account.get(key), str) and account[key] for key in ("user", "credential_target")):
            raise ProjectError("PROJECT_CONFIG_INVALID", "项目账号配置无效。")
    if not isinstance(profile.get("business_scope_id"), str) or not profile["business_scope_id"].endswith("|database:" + project_id):
        raise ProjectError("PROJECT_CONFIG_INVALID", "项目业务作用域无效。")
    from .settings import reject_secrets
    reject_secrets(profile)
    return profile


def _load_context(role: str) -> ProjectContext | None:
    active = project_active_settings()
    if active and active.get("schema_version") == CONTRACT_VERSION:
        profile = load_profile(active.get("project_id", ""), active.get("profile_revision", ""))
        configs = {name: {**profile["database"], **profile["accounts"][name], "environment": profile["environment"]} for name in ("look", "write")}
        return ProjectContext(project_id=profile["project_id"], database_scope=profile["project_id"],
            business_scope_id=profile["business_scope_id"], environment=profile["environment"],
            profile_revision=profile["revision"], activation_id=active.get("activation_id", ""),
            refresh_generation=active.get("refresh_generation", ""), database_configs=configs,
            platform_binding=profile["platform_binding"], platform_version={**profile.get("platform_version", {}), "refresh": profile.get("refresh", {})},
            managed=True, name=profile["name"])
    if active is not None and active.get("schema_version") != 1:
        raise ProjectError("PROJECT_PROTOCOL_UNSUPPORTED", "项目协议版本不兼容，请成套更新 Look / Write。")
    base = _base_config(role)
    if active:
        if set(active) != {"schema_version", "revision", "host", "port", "database", "accounts"}:
            raise ProjectError("PROJECT_CONFIG_INVALID", "旧统一连接配置格式无效。")
        if not isinstance(active["accounts"], dict) or set(active["accounts"]) != {"look", "write"}:
            raise ProjectError("PROJECT_CONFIG_INVALID", "旧统一连接账号无效。")
        for account in active["accounts"].values():
            if not isinstance(account, dict) or set(account) != {"user", "credential_target"}:
                raise ProjectError("PROJECT_CONFIG_INVALID", "账号配置不能覆盖数据库地址或包含额外字段。")
        configs = {name: {**{key: active[key] for key in ("host", "port", "database")}, **active["accounts"][name]} for name in ("look", "write")}
    elif all(base.get(key) not in (None, "") for key in ("host", "port", "database")):
        configs = {role: base}
    else:
        return None
    scope = database_scope(configs[role])
    return ProjectContext(scope, scope, _business_scope(scope), database_configs=configs,
                          profile_revision=digest(active or base), name=configs[role]["database"])


def get_context(required: bool = False, role: str = "look") -> ProjectContext | None:
    result = _PINNED.get() if _PIN_SET.get() else _load_context(role)
    if required and result is None:
        raise ProjectError("PROJECT_NOT_CONFIGURED", "请先选择或配置项目。")
    return result


current_context = get_context


@contextmanager
def pin_context(context: ProjectContext | None):
    token, set_token = _PINNED.set(context), _PIN_SET.set(True)
    try:
        yield context
    finally:
        _PIN_SET.reset(set_token)
        _PINNED.reset(token)


def assert_active(context: ProjectContext | None) -> None:
    active = project_active_settings() or {}
    if context is None or not context.managed:
        if active.get("schema_version") == CONTRACT_VERSION:
            raise ProjectError("PROJECT_CHANGED", "项目已启用或切换，请重新读取目标。")
        return
    if any(active.get(key) != getattr(context, key) for key in ("project_id", "profile_revision", "activation_id", "refresh_generation")):
        raise ProjectError("PROJECT_CHANGED", "项目已切换，请在当前项目重新读取目标。")


def assert_binding(binding: dict | None, context: ProjectContext | None = None) -> None:
    context = context or get_context(role="write")
    if context is None or not context.managed:
        if binding and binding.get("activation_id"):
            raise ProjectError("PROJECT_CHANGED", "该流程属于已失效的项目激活。")
        return
    assert_active(context)
    if not isinstance(binding, dict) or any(binding.get(key) != value for key, value in context.binding().items()):
        raise ProjectError("PROJECT_BINDING_MISMATCH", "流程或审批不属于本次项目激活；请重新读取目标并创建流程。")


@contextmanager
def operation_gate():
    if _GATE.get():
        yield
        return
    if not _THREAD_GATE.acquire(blocking=False):
        raise ProjectError("PROJECT_BUSY", "另一个项目操作正在提交，请稍后重试。")
    stream, locked, token = None, False, None
    try:
        path = config_root() / "operation.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a+b")
        if path.stat().st_size == 0:
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
            locked = True
        except OSError as exc:
            raise ProjectError("PROJECT_BUSY", "另一个项目操作正在提交，请稍后重试。") from exc
        token = _GATE.set(True)
        yield
    finally:
        if token is not None:
            _GATE.reset(token)
        if stream is not None:
            if locked:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
        _THREAD_GATE.release()


@contextmanager
def project_operation(expected_binding: dict | None = None, role: str = "write"):
    with operation_gate():
        pinned = get_context(role=role)
        assert_active(pinned)
        context = _load_context(role)
        if expected_binding is not None:
            assert_binding(expected_binding, context)
        with pin_context(context):
            yield context
        assert_active(context)


@contextmanager
def request_context(role="look"):
    context = get_context(role=role)
    with pin_context(context):
        yield context
    assert_active(context)


def assert_ready(resources, ctx: ProjectContext | None = None, object_ids: dict | None = None):
    ctx = ctx or get_context(role="write")
    if ctx is None or not ctx.managed:
        return
    assert_active(ctx)
    manifest = read_json(ctx.snapshot_root("status").parent / "manifest.json", max_bytes=16 * 1024 * 1024) or {}
    if manifest.get("activation_id") != ctx.activation_id or manifest.get("refresh_generation") != ctx.refresh_generation:
        raise ProjectError("PROJECT_RESOURCES_NOT_READY", "本次项目资料尚未刷新，请查看刷新状态。")
    missing = []
    for item in resources:
        name = item.get("resource") if isinstance(item, dict) else item
        result = manifest.get("resources", {}).get(name, {})
        requested = list((object_ids or {}).get(name, []))
        if isinstance(item, dict) and item.get("object_id"):
            requested.append(item["object_id"])
        objects = result.get("objects", {})
        if isinstance(objects, list):
            objects = {str(obj.get("id") or obj.get("route")): obj for obj in objects if isinstance(obj, dict)}
        if requested and all(isinstance(objects.get(key), dict) and objects[key].get("status") == "ready"
                             and objects[key].get("logic_complete", True) is not False for key in requested):
            continue
        if not requested and result.get("status") == "ready":
            continue
        missing.append({"resource": name, "object_ids": requested, "status": result.get("status", "pending")})
    if missing:
        raise ProjectError("PROJECT_RESOURCES_NOT_READY", "所需项目资料尚未完整获取；请刷新失败项后重试。", {"missing_resources": missing})
