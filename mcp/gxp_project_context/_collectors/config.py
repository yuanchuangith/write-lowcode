from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import keyring


class ConfigurationError(RuntimeError):
    pass


def default_config_path() -> Path:
    configured = os.environ.get("GXP_LOWCODE_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        base = Path(app_data) if app_data else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg_config).expanduser() if xdg_config else Path.home() / ".config"
    return base / "GxpLowcodeReadonly" / "database.json"


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    database: str
    user: str
    credential_target: str = "Codex.GxpLowcodeReadonly"
    connect_timeout_seconds: int = 8
    read_timeout_seconds: int = 10
    max_execution_time_ms: int = 5000
    pool_size: int = 4

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DatabaseConfig":
        required = ("host", "port", "database", "user")
        missing = [name for name in required if value.get(name) in (None, "")]
        if missing:
            raise ConfigurationError(
                "Database configuration is missing: " + ", ".join(missing)
            )
        try:
            return cls(
                host=str(value["host"]).strip(),
                port=int(value["port"]),
                database=str(value["database"]).strip(),
                user=str(value["user"]).strip(),
                credential_target=str(
                    value.get("credential_target")
                    or "Codex.GxpLowcodeReadonly"
                ).strip(),
                connect_timeout_seconds=max(
                    1, min(int(value.get("connect_timeout_seconds", 8)), 30)
                ),
                read_timeout_seconds=max(
                    1, min(int(value.get("read_timeout_seconds", 10)), 60)
                ),
                max_execution_time_ms=max(
                    100, min(int(value.get("max_execution_time_ms", 5000)), 10000)
                ),
                pool_size=max(1, min(int(value.get("pool_size", 4)), 8)),
            )
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"Invalid database configuration: {exc}") from exc

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_config(path: Path | None = None) -> DatabaseConfig:
    config_path = path or default_config_path()
    if not config_path.exists():
        if path is None:
            from .connection_settings import active_settings, overlay_database
            if active_settings() is not None:
                return DatabaseConfig.from_dict(overlay_database({}, "look"))
        raise ConfigurationError(
            f"Connection configuration was not found at {config_path}. "
            "Run scripts/configure_connection.py first."
        )
    try:
        value = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Cannot read {config_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("Database configuration must be a JSON object")
    if path is None:
        from .connection_settings import overlay_database
        value = overlay_database(value, "look")
    return DatabaseConfig.from_dict(value)


def save_config(config: DatabaseConfig, path: Path | None = None) -> Path:
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(config.public_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(config_path)
    return config_path


def set_password(config: DatabaseConfig, password: str) -> None:
    if not password:
        raise ConfigurationError("Password cannot be empty")
    keyring.set_password(config.credential_target, config.user, password)


def load_password(config: DatabaseConfig) -> str:
    password_file = os.environ.get("GXP_LOWCODE_DB_PASSWORD_FILE")
    # Shared profiles use the exact OS credential tested by the settings window.
    # Keep password-file overrides only for legacy / server configurations.
    if password_file and not config.credential_target.startswith("GxpLowcode.Connection."):
        try:
            password = Path(password_file).expanduser().read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise ConfigurationError(
                "Cannot read the configured database password file."
            ) from exc
        if not password:
            raise ConfigurationError("The configured database password file is empty.")
        return password
    password = keyring.get_password(config.credential_target, config.user)
    if not password:
        raise ConfigurationError(
            "No database password is stored in the operating-system credential store. "
            "Run the platform-specific configure_connection script first."
        )
    return password


def delete_password(config: DatabaseConfig) -> None:
    try:
        keyring.delete_password(config.credential_target, config.user)
    except keyring.errors.PasswordDeleteError:
        return
