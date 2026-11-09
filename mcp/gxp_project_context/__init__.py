"""Shared, independently packaged Look/Write project contract."""
from .context import (
    CONTRACT_VERSION, ProjectContext, ProjectError, active_path, assert_active,
    assert_binding, assert_ready, atomic_json, config_root, current_context,
    database_scope, get_context, operation_gate, pin_context, project_operation,
    read_json, request_context, runtime_root,
)
from ._compat import (
    current_database_scope, profiles_root, resolve_active, list_profiles,
    save_profile, load_profile, _atomic_json, _read, _digest,
)

__all__ = [name for name in globals() if not name.startswith("_")]
