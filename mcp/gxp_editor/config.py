from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import keyring


class ConfigurationError(RuntimeError):
    pass


ACCOUNT_MODES = {"dedicated", "shared_restricted", "shared_unrestricted"}
DEFAULT_BACKEND_REPO = r"G:\hoyi\updateWeb\gxp2.web"


def default_config_path() -> Path:
    configured = os.environ.get("GXP_LOWCODE_EDITOR_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    app_data = os.environ.get("APPDATA")
    base = Path(app_data) if app_data else Path.home() / ".config"
    return base / "GxpLowcodeEditor" / "database.json"


def runtime_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".local" / "share"
    path = base / "GxpLowcodeEditor"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    database: str
    user: str
    credential_target: str = "Codex.GxpLowcodeEditor.Database"
    account_mode: str = "dedicated"
    expected_grants_sha256: str = ""
    connect_timeout_seconds: int = 8
    read_timeout_seconds: int = 10
    write_timeout_seconds: int = 10

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DatabaseConfig":
        required = ("host", "port", "database", "user")
        missing = [name for name in required if value.get(name) in (None, "")]
        if missing:
            raise ConfigurationError("Database configuration is missing: " + ", ".join(missing))
        account_mode = str(value.get("account_mode") or "dedicated").strip().lower()
        if account_mode not in ACCOUNT_MODES:
            raise ConfigurationError(
                "database.account_mode must be dedicated, shared_restricted, or shared_unrestricted"
            )
        expected_grants_sha256 = str(value.get("expected_grants_sha256") or "").strip().lower()
        if expected_grants_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_grants_sha256):
            raise ConfigurationError("database.expected_grants_sha256 must be a SHA-256 hex digest")
        return cls(
            host=str(value["host"]).strip(),
            port=int(value["port"]),
            database=str(value["database"]).strip(),
            user=str(value["user"]).strip(),
            credential_target=str(value.get("credential_target") or cls.credential_target).strip(),
            account_mode=account_mode,
            expected_grants_sha256=expected_grants_sha256,
            connect_timeout_seconds=max(1, min(int(value.get("connect_timeout_seconds", 8)), 30)),
            read_timeout_seconds=max(1, min(int(value.get("read_timeout_seconds", 10)), 60)),
            write_timeout_seconds=max(1, min(int(value.get("write_timeout_seconds", 10)), 60)),
        )


@dataclass(frozen=True)
class RuntimeReferenceConfig:
    directory: str
    source_mode: str = "development_build"
    target_framework: str = "net6.0"
    dotnet_command: str = "dotnet"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeReferenceConfig":
        directory = str(value.get("directory") or "").strip()
        if not directory:
            raise ConfigurationError("runtime_references.directory is required")
        source_mode = str(value.get("source_mode") or "development_build").strip()
        if source_mode not in {"development_build", "deployment_snapshot"}:
            raise ConfigurationError(
                "runtime_references.source_mode must be development_build or deployment_snapshot"
            )
        target_framework = str(value.get("target_framework") or "net6.0").strip()
        if not target_framework or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for ch in target_framework):
            raise ConfigurationError("runtime_references.target_framework is invalid")
        dotnet_command = str(value.get("dotnet_command") or "dotnet").strip()
        if not dotnet_command:
            raise ConfigurationError("runtime_references.dotnet_command is required")
        return cls(
            directory=directory,
            source_mode=source_mode,
            target_framework=target_framework,
            dotnet_command=dotnet_command,
        )

    @property
    def path(self) -> Path:
        return Path(self.directory).expanduser().resolve()


@dataclass(frozen=True)
class EditorConfig:
    database: DatabaseConfig
    frontend_repo: str
    compiler_bridge: str
    runtime_references: RuntimeReferenceConfig
    approval_hmac_target: str = "Codex.GxpLowcodeEditor.Approval"
    approval_ttl_minutes: int = 15
    backend_repo: str = DEFAULT_BACKEND_REPO

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EditorConfig":
        database = value.get("database")
        if not isinstance(database, dict):
            raise ConfigurationError("database must be a JSON object")
        frontend_repo = str(value.get("frontend_repo") or "").strip()
        if not frontend_repo:
            raise ConfigurationError("frontend_repo is required")
        runtime_references = value.get("runtime_references")
        if not isinstance(runtime_references, dict):
            raise ConfigurationError("runtime_references must be a JSON object")
        compiler_bridge = str(
            value.get("compiler_bridge")
            or "dist/action-design-compiler/bridge.js"
        ).strip()
        return cls(
            database=DatabaseConfig.from_dict(database),
            frontend_repo=frontend_repo,
            compiler_bridge=compiler_bridge,
            runtime_references=RuntimeReferenceConfig.from_dict(runtime_references),
            approval_hmac_target=str(value.get("approval_hmac_target") or cls.approval_hmac_target).strip(),
            approval_ttl_minutes=max(1, min(int(value.get("approval_ttl_minutes", 15)), 60)),
            backend_repo=str(value.get("backend_repo") or DEFAULT_BACKEND_REPO).strip() or DEFAULT_BACKEND_REPO,
        )

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def compiler_path(self) -> Path:
        path = Path(self.compiler_bridge)
        if path.is_absolute():
            return path
        return Path(self.frontend_repo) / path


def load_config(path: Path | None = None) -> EditorConfig:
    config_path = path or default_config_path()
    if not config_path.exists():
        raise ConfigurationError(
            f"Editor configuration was not found at {config_path}. "
            "Run scripts/configure_connection.ps1 first."
        )
    try:
        value = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Cannot read {config_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("Editor configuration must be a JSON object")
    return EditorConfig.from_dict(value)


def save_config(config: EditorConfig, path: Path | None = None) -> Path:
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(config.public_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(config_path)
    return config_path


def set_secret(target: str, user: str, value: str) -> None:
    if not value:
        raise ConfigurationError("Secret cannot be empty")
    keyring.set_password(target, user, value)


def load_secret(target: str, user: str) -> str:
    value = keyring.get_password(target, user)
    if not value:
        raise ConfigurationError("The required Windows Credential Manager secret is not configured")
    return value


def ensure_approval_secret(config: EditorConfig) -> str:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "local-user"
    value = keyring.get_password(config.approval_hmac_target, user)
    if not value:
        value = secrets.token_hex(32)
        keyring.set_password(config.approval_hmac_target, user, value)
    return value


def approval_secret(config: EditorConfig) -> str:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "local-user"
    return load_secret(config.approval_hmac_target, user)
