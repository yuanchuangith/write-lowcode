"""Exclusive reusable restore workspaces; candidate source is never cached."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator

from .execution_lock import ExecutionBusyError, _os_lock


RESTORE_FILES = (
    "project.assets.json", "project.nuget.cache", "Candidate.csproj.nuget.dgspec.json",
    "Candidate.csproj.nuget.g.props", "Candidate.csproj.nuget.g.targets",
)
WORKSPACE_SCHEMA = 2


class WorkspaceBusyError(RuntimeError):
    pass


def _checked_target(path: Path, pool_root: Path) -> Path:
    resolved, boundary = path.resolve(), pool_root.resolve()
    if resolved == boundary or not resolved.is_relative_to(boundary):
        raise ValueError("Runtime workspace path escaped its cache root")
    return path


def _remove(path: Path, pool_root: Path) -> None:
    _checked_target(path, pool_root)
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _restore_manifest(root: Path) -> dict[str, str]:
    import hashlib

    if (root / "obj").resolve() != root / "obj":
        raise ValueError("Restore cache must not be a linked directory")
    result = {}
    for name in RESTORE_FILES:
        path = root / "obj" / name
        if path.is_symlink():
            raise ValueError("Restore cache contains a symbolic link")
        if path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if "project.assets.json" not in result:
        raise ValueError("dotnet restore did not create project.assets.json")
    return result


class RuntimeWorkspace:
    def __init__(self, root: Path, pool_root: Path, environment_key: str, request_id: str):
        self.root = root
        self.pool_root = pool_root
        self.environment_key = environment_key
        self.request_id = request_id
        self.cache_hit = False
        self.wait_ms = 0.0
        self._restore_ready = False
        self._restored_manifest: dict[str, str] | None = None
        self.active = False

    @property
    def project(self) -> Path:
        return self.root / "Candidate.csproj"

    @property
    def source(self) -> Path:
        return self.root / "Candidate.cs"

    @property
    def nuget_config(self) -> Path:
        return self.root / "NuGet.Config"

    def start(self, project_text: str, nuget_text: str) -> None:
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise ValueError("Runtime workspace slot must not be a symbolic link")
        state = _read_json(self.root / "state.json")
        try:
            self.cache_hit = (
                state.get("schema_version") == WORKSPACE_SCHEMA
                and state.get("environment_key") == self.environment_key
                and state.get("status") == "restore_ready"
                and not self.source.exists()
                and not self.project.is_symlink()
                and not self.nuget_config.is_symlink()
                and self.project.read_text(encoding="utf-8") == project_text
                and self.nuget_config.read_text(encoding="utf-8") == nuget_text
                and state.get("restore_manifest") == _restore_manifest(self.root)
            )
        except (OSError, ValueError):
            self.cache_hit = False
        if not self.cache_hit:
            _remove(self.root, self.pool_root)
            self.root.mkdir(parents=True)
            self.project.write_text(project_text, encoding="utf-8")
            self.nuget_config.write_text(nuget_text, encoding="utf-8")
        _write_json(self.root / "state.json", {
            "schema_version": WORKSPACE_SCHEMA, "environment_key": self.environment_key, "status": "dirty",
        })
        _write_json(self.root / "lease.json", {"request_id": self.request_id, "process_id": os.getpid()})
        self.active = True
        self.scrub_outputs()
        if self.cache_hit:
            self.capture_restore_state()

    def scrub_outputs(self) -> None:
        _remove(self.source, self.pool_root)
        _remove(self.root / "bin", self.pool_root)
        obj = self.root / "obj"
        if obj.is_symlink():
            _remove(obj, self.pool_root)
        elif obj.is_dir():
            _checked_target(obj, self.pool_root)
            for item in obj.iterdir():
                if item.name not in RESTORE_FILES or not item.is_file() or item.is_symlink():
                    _remove(item, self.pool_root)

    def capture_restore_state(self) -> None:
        """Remember verified restore metadata, never candidate validation evidence."""
        self._restored_manifest = _restore_manifest(self.root)

    def mark_restore_ready(self) -> None:
        # The caller has completed the final DLL check. A source-only compile
        # failure is allowed, but the restore metadata must remain identical.
        if self._restored_manifest is None or _restore_manifest(self.root) != self._restored_manifest:
            raise ValueError("Restore metadata changed during C# compilation")
        self._restore_ready = True

    def close(self) -> None:
        self.scrub_outputs()
        if self._restore_ready:
            manifest = _restore_manifest(self.root)
            if manifest != self._restored_manifest:
                raise ValueError("Restore metadata changed before workspace cleanup completed")
            _write_json(self.root / "state.json", {
                "schema_version": WORKSPACE_SCHEMA, "environment_key": self.environment_key,
                "status": "restore_ready", "restore_manifest": manifest,
            })
        # Leave the dirty lease discoverable if scrubbing failed, so the parent
        # can retry cleanup after the probe exits and the OS releases handles.
        (self.root / "lease.json").unlink(missing_ok=True)


@contextmanager
def lease_workspace(
    pool_root: Path, environment_key: str, request_id: str, *, project_text: str,
    nuget_text_factory: Any, wait_seconds: float = 2.0, slots: int = 2,
) -> Iterator[RuntimeWorkspace]:
    if not re.fullmatch(r"[0-9a-f]{64}", environment_key):
        raise ValueError("Invalid runtime workspace environment key")
    if not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("Invalid runtime workspace request identity")
    pool_root = pool_root.resolve()
    parent = pool_root / environment_key
    parent.mkdir(parents=True, exist_ok=True)
    _checked_target(parent, pool_root)
    started = time.monotonic()
    deadline = started + max(0.0, wait_seconds)
    while True:
        for index in range(max(1, slots)):
            root = parent / f"slot-{index}"
            _checked_target(root, pool_root)
            stack = ExitStack()
            try:
                stack.enter_context(_os_lock(root, ".workspace.lock", ExecutionBusyError, "Runtime workspace is busy"))
            except ExecutionBusyError:
                stack.close()
                continue
            workspace = RuntimeWorkspace(root, pool_root, environment_key, request_id)
            workspace.wait_ms = round((time.monotonic() - started) * 1000, 3)
            with stack:
                try:
                    workspace.start(project_text, nuget_text_factory(root))
                    yield workspace
                except BaseException:
                    workspace._restore_ready = False
                    raise
                finally:
                    if workspace.active and workspace.root.exists():
                        workspace.close()
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkspaceBusyError("All local C# workspace slots are busy; retry after the active compile finishes")
        time.sleep(min(0.05, remaining))


def cleanup_interrupted_request(pool_root: Path, request_id: str) -> None:
    """Best effort after a dead probe; never touches a live slot or business data."""
    if not re.fullmatch(r"[0-9a-f]{32}", request_id) or not pool_root.is_dir():
        return
    pool_root = pool_root.resolve()
    for lease in pool_root.glob("*/slot-*/lease.json"):
        if _read_json(lease).get("request_id") != request_id:
            continue
        root = lease.parent
        try:
            _checked_target(root, pool_root)
            if root.is_symlink() or root.resolve() != root:
                continue
            with _os_lock(root, ".workspace.lock", ExecutionBusyError, "Runtime workspace is busy"):
                if _read_json(lease).get("request_id") == request_id:
                    workspace = RuntimeWorkspace(root, pool_root, "", request_id)
                    _write_json(root / "state.json", {"status": "dirty"})
                    workspace.close()
        except (OSError, ValueError, ExecutionBusyError):
            continue
