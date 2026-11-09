from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import keyring


CPM_CREDENTIAL_TARGET = "Codex.GxpLowcodeReadonly.Cpm"
DEFAULT_TTL_SECONDS = 5400
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_CONCURRENCY = 10


def _config_root() -> Path:
    override = os.environ.get("GXP_LOWCODE_CONFIG_ROOT")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        value = os.environ.get("APPDATA")
        return Path(value) if value else Path.home() / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    value = os.environ.get("XDG_CONFIG_HOME")
    return Path(value).expanduser() if value else Path.home() / ".config"


def _data_root() -> Path:
    override = os.environ.get("GXP_LOWCODE_RUNTIME_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        value = os.environ.get("LOCALAPPDATA")
        return Path(value) if value else Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    value = os.environ.get("XDG_DATA_HOME")
    return Path(value).expanduser() if value else Path.home() / ".local" / "share"


def cpm_config_path() -> Path:
    override = os.environ.get("GXP_LOWCODE_CPM_CONFIG")
    return Path(override) if override else _config_root() / "GxpLowcodeReadonly" / "cpm.json"


def cpm_runtime_root() -> Path:
    override = os.environ.get("GXP_LOWCODE_RUNTIME_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return _data_root() / "GxpLowcodeReadonly"


def cpm_status_path(config=None) -> Path:
    return _cpm_cache_root(config) / "cpm-refresh-status.json"


def cpm_lock_path(config=None) -> Path:
    return _cpm_cache_root(config) / "cpm-refresh.lock"


def _cpm_cache_root(config) -> Path:
    if config is not None and getattr(config, "cache_scope", ""):
        directory = Path(config.snapshot_dir)
        return directory.with_name(directory.name + ".state")
    return cpm_runtime_root()


@dataclass(frozen=True)
class CpmConfig:
    platform_url: str
    account: str
    node_path: str
    cli_path: str
    snapshot_dir: str
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    concurrency: int = DEFAULT_CONCURRENCY
    cache_scope: str = ""
    credential_target: str = CPM_CREDENTIAL_TARGET
    app_id: str = ""
    site_out_id: str = ""
    api_prefix: str = "/gxp2"
    fresh_pull: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CpmConfig":
        forbidden = {key for key in raw if "password" in key.lower()}
        if forbidden:
            raise ValueError("cpm.json 不得包含密码字段")
        config = cls(
            platform_url=str(raw.get("platform_url", "")).rstrip("/"),
            account=str(raw.get("account", "")),
            node_path=str(raw.get("node_path", "")),
            cli_path=str(raw.get("cli_path", "")),
            snapshot_dir=str(raw.get("snapshot_dir", "")),
            ttl_seconds=int(raw.get("ttl_seconds", DEFAULT_TTL_SECONDS)),
            timeout_seconds=int(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            concurrency=int(raw.get("concurrency", DEFAULT_CONCURRENCY)),
            credential_target=str(raw.get("credential_target") or CPM_CREDENTIAL_TARGET),
            app_id=str(raw.get("app_id") or ""), site_out_id=str(raw.get("site_out_id") or ""),
            api_prefix=str(raw.get("api_prefix") or "/gxp2"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.cache_scope and not re.fullmatch(r"[a-f0-9]{64}", self.cache_scope):
            raise ValueError("Invalid database cache scope")
        if not self.platform_url.startswith(("http://", "https://")):
            raise ValueError("platform_url 必须是 HTTP(S) 地址")
        if not self.account:
            raise ValueError("account 不能为空")
        for name, value in (
            ("node_path", self.node_path),
            ("cli_path", self.cli_path),
            ("snapshot_dir", self.snapshot_dir),
        ):
            if not value or not Path(value).is_absolute():
                raise ValueError(f"{name} 必须是绝对路径")
        if self.ttl_seconds < 1 or self.timeout_seconds < 1 or self.concurrency < 1:
            raise ValueError("TTL、超时和并发数必须是正整数")


def bind_cpm_cache(config: CpmConfig, database_config=None) -> CpmConfig:
    if config.cache_scope:
        return config  # A request keeps its originally loaded database namespace.
    from .cache_scope import database_cache_scope
    scope = database_cache_scope(database_config)
    if scope is None:
        return config
    from gxp_project_context.context import get_context
    context = get_context(role="look")
    if context is not None and context.managed and context.database_scope == scope:
        binding = context.platform_binding
        return replace(config, snapshot_dir=str(context.snapshot_root("cpm")), cache_scope=scope,
                       platform_url=str(binding.get("platform_url") or config.platform_url),
                       account=str(binding.get("account") or config.account),
                       credential_target=str(binding.get("credential_target") or config.credential_target),
                       app_id=str(binding.get("app_id") or ""), site_out_id=str(binding.get("site_out_id") or ""),
                       api_prefix=str(binding.get("form_config", {}).get("api_prefix") or config.api_prefix))
    return replace(config, snapshot_dir=str(Path(config.snapshot_dir) / "environments" / scope), cache_scope=scope)


def load_cpm_config(database_config=None) -> CpmConfig:
    path = cpm_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"CPM 配置不存在: {path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("cpm.json 不是有效 UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("cpm.json 顶层必须是对象")
    return bind_cpm_cache(CpmConfig.from_dict(raw), database_config)


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def save_cpm_config(config: CpmConfig) -> None:
    config.validate()
    value = asdict(config)
    value.pop("cache_scope", None)
    value.pop("fresh_pull", None)
    directory = Path(config.snapshot_dir)
    if config.cache_scope and directory.parent.name == "environments" and directory.name == config.cache_scope:
        value["snapshot_dir"] = str(directory.parent.parent)
    elif config.cache_scope:
        from gxp_project_context.context import get_context
        context = get_context(role="look")
        if context is not None and context.managed and directory == context.snapshot_root("cpm"):
            original = json.loads(cpm_config_path().read_text(encoding="utf-8"))
            value["snapshot_dir"] = original["snapshot_dir"]
            for key in ("platform_url", "account", "credential_target", "app_id", "site_out_id", "api_prefix"):
                if key in original:
                    value[key] = original[key]
                else:
                    value.pop(key, None)
    _atomic_json(cpm_config_path(), value)


def upgrade_cpm_runtime(runtime_root: str, node_path: str, cli_path: str) -> bool:
    """Retarget an existing managed CLI binding after installation; leave credentials untouched."""
    try:
        config = load_cpm_config()
    except FileNotFoundError:
        return False
    managed = Path(runtime_root).resolve() / "cpm-cli"
    try:
        Path(config.cli_path).resolve().relative_to(managed)
        Path(cli_path).resolve().relative_to(managed)
    except ValueError:
        return False
    updated = replace(config, node_path=node_path, cli_path=cli_path)
    if updated == config:
        return False
    save_cpm_config(updated)
    return True


def set_cpm_password(account: str, password: str) -> None:
    if not account or not password:
        raise ValueError("账号和密码不能为空")
    keyring.set_password(CPM_CREDENTIAL_TARGET, account, password)


def get_cpm_password(config: CpmConfig) -> str:
    password = keyring.get_password(config.credential_target, config.account)
    if not password:
        raise RuntimeError("CPM_PASSWORD_MISSING")
    return password


def delete_cpm_password(account: str) -> None:
    try:
        keyring.delete_password(CPM_CREDENTIAL_TARGET, account)
    except keyring.errors.PasswordDeleteError:
        pass


def write_cpm_config(raw: dict[str, Any], password: str | None = None) -> CpmConfig:
    """Configuration helper for the local setup script; password never enters JSON."""
    config = CpmConfig.from_dict(raw)
    save_cpm_config(config)
    if password is not None:
        set_cpm_password(config.account, password)
    return config
