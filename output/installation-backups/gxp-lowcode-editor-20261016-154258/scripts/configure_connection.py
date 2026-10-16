from __future__ import annotations

import getpass
import sys
from dataclasses import replace
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))

from gxp_editor.config import (
    ConfigurationError,
    DatabaseConfig,
    DEFAULT_BACKEND_REPO,
    EditorConfig,
    RuntimeReferenceConfig,
    ensure_approval_secret,
    load_config,
    save_config,
    set_secret,
)
from gxp_editor.db import EditorDatabase
from gxp_editor.repository import EditorRepository


def ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def main() -> int:
    try:
        current = load_config()
    except ConfigurationError:
        current = None
    database_current = current.database if current else None
    host = ask("Database host", database_current.host if database_current else "")
    port = int(ask("Database port", str(database_current.port) if database_current else "3306"))
    database_name = ask("Database name", database_current.database if database_current else "")
    user = ask("Editor database user", database_current.user if database_current else "")
    account_mode = ask(
        "Account mode (dedicated/shared_restricted/shared_unrestricted)",
        database_current.account_mode if database_current else "dedicated",
    ).lower()
    if account_mode not in {"dedicated", "shared_restricted", "shared_unrestricted"}:
        raise ConfigurationError(
            "Account mode must be dedicated, shared_restricted, or shared_unrestricted"
        )
    if account_mode == "shared_restricted":
        print(
            "WARNING: shared_restricted only constrains SQL issued through this MCP. "
            "The database account retains all of its permissions outside the plugin."
        )
        if ask("Type SHARED to confirm the shared-account risk", "") != "SHARED":
            raise ConfigurationError("Shared account confirmation was not provided")
    if account_mode == "shared_unrestricted":
        print(
            "DANGER: shared_unrestricted accepts global and server-level database privileges. "
            "The plugin still restricts itself to fixed SQL templates and pins SHOW GRANTS, "
            "but it cannot reduce what this account can do in other clients."
        )
        if (
            ask(
                "Type ALLOW PRIVILEGED SHARED to confirm the privileged-account risk",
                "",
            )
            != "ALLOW PRIVILEGED SHARED"
        ):
            raise ConfigurationError("Privileged shared-account confirmation was not provided")
    credential_target = ask(
        "Windows database credential target",
        database_current.credential_target if database_current else "Codex.GxpLowcodeEditor.Database",
    )
    frontend_repo = ask(
        "ActionDesign frontend repository",
        current.frontend_repo if current else r"G:\hoyi\updateComponents\gxp2.components",
    )
    backend_repo = ask(
        "Read-only backend source repository (same as gxp-lowcode-debug)",
        current.backend_repo if current else DEFAULT_BACKEND_REPO,
    )
    compiler_bridge = ask(
        "Compiler bridge path relative to frontend repository",
        current.compiler_bridge if current else "dist/action-design-compiler/bridge.js",
    )
    runtime_reference_dir = ask(
        "Read-only backend runtime reference directory (required to compile nonblank candidate C#)",
        current.runtime_references.directory if current else "",
    )
    runtime_source_mode = ask(
        "Runtime reference source mode (development_build/deployment_snapshot)",
        current.runtime_references.source_mode if current else "development_build",
    )
    runtime_target_framework = ask(
        "Backend target framework",
        current.runtime_references.target_framework if current else "net6.0",
    )

    database_password = getpass.getpass("Database password (hidden): ")
    config = EditorConfig(
        database=DatabaseConfig(
            host=host,
            port=port,
            database=database_name,
            user=user,
            credential_target=credential_target,
            account_mode=account_mode,
        ),
        frontend_repo=frontend_repo,
        backend_repo=backend_repo,
        compiler_bridge=compiler_bridge,
        runtime_references=RuntimeReferenceConfig.from_dict(
            {
                "directory": runtime_reference_dir,
                "source_mode": runtime_source_mode,
                "target_framework": runtime_target_framework,
            }
        ),
    )
    set_secret(credential_target, user, database_password)
    del database_password
    status = EditorDatabase(config.database).status()
    if account_mode in {"shared_restricted", "shared_unrestricted"}:
        grant_status = EditorRepository(EditorDatabase(config.database)).grant_status(
            account_mode=account_mode,
        )
        if account_mode == "shared_restricted" and not grant_status["shared_policy_eligible"]:
            raise ConfigurationError(
                "Shared account has global or server-level grants that this editor refuses"
            )
        config = replace(
            config,
            database=replace(
                config.database,
                expected_grants_sha256=grant_status["grants_sha256"],
            ),
        )
        print(f"Pinned SHOW GRANTS fingerprint: {grant_status['grants_sha256']}")
        if account_mode == "shared_unrestricted":
            print(
                "Privileged shared-account override enabled. Any SHOW GRANTS drift will still block writes."
            )
    path = save_config(config)
    ensure_approval_secret(config)
    print(f"Configuration saved outside the plugin: {path}")
    print(f"Database read check succeeded: {status.get('server_version', 'unknown')}")
    print("No database write was executed. Run editor_status to inspect indexes, account safety, canvas compiler, and runtime snapshot gates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
