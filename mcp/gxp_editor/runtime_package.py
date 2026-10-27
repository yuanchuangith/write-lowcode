"""Byte-verified, process-bound runtime packages and interpreter leases.

This module deliberately uses only the standard library: both the launcher and
setup use the same coordination protocol before loading optional dependencies.
Packages are never automatically removed or repaired from another version.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import runpy
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "runtime-manifest.json"
MANIFEST_SCHEMA = 1
RUNTIME_SCRIPT_NAMES = {
    "approval_app.py", "abort_session_app.py", "configure_connection.py",
    "database_settings.py", "preview_component_canvas.py", "preview_saved_approval.py",
    "start_mcp.ps1", "start_mcp.cmd",
}
_REQUIRED = {"mcp/server.py", "mcp/gxp_editor/runtime_package.py",
             "scripts/approval_app.py", "scripts/abort_session_app.py",
             ".codex-plugin/plugin.json", "requirements.txt"}


class RuntimeResourceError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = {"reconnect_required": True, **(details or {})}


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_storage_root() -> Path:
    base = Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA") else Path.home() / ".local/share"
    return base / "GxpLowcodeEditor" / "runtime"


def _storage(value=None) -> Path:
    return Path(value).resolve() if value is not None else package_storage_root().resolve()


@contextlib.contextmanager
def runtime_coordination(storage_root=None, *, timeout_seconds=10.0):
    """Serialize package activation, lease changes and shared environment writes."""
    root = _storage(storage_root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "coordination.lock").open("a+b") as stream:
        # Windows byte-range locks also deny reads by another handle. Inspect
        # metadata here; touching byte zero before acquiring it breaks waiters.
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeResourceError("plugin_runtime_coordination_busy",
                        "Runtime preparation or environment update is already in progress.") from exc
                time.sleep(0.025)
        try:
            yield root
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def runtime_files(root: Path) -> list[str]:
    """Include late imports, Node rules, native bridge modules and visual assets."""
    root = Path(root).resolve()
    paths = [root / ".codex-plugin/plugin.json", root / "requirements.txt"]
    paths.extend(path for path in (root / "mcp").rglob("*")
                 if path.is_file() and path.suffix in {".py", ".cjs", ".js", ".json"}
                 and "__pycache__" not in path.parts)
    paths.extend(path for path in (root / "assets").rglob("*") if path.is_file())
    paths.extend(root / "scripts" / name for name in RUNTIME_SCRIPT_NAMES
                 if (root / "scripts" / name).is_file())
    return sorted({path.relative_to(root).as_posix() for path in paths})


def _safe_file(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if (not relative or "\\" in relative or parts.is_absolute() or ":" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise RuntimeResourceError("plugin_runtime_manifest_invalid", "Unsafe runtime manifest path.")
    path = root.joinpath(*parts.parts)
    if any(item.is_symlink() for item in (path, *path.parents) if item != root.parent):
        raise RuntimeResourceError("plugin_runtime_manifest_invalid", "Runtime resources cannot be symbolic links.")
    if not path.resolve().is_relative_to(root):
        raise RuntimeResourceError("plugin_runtime_manifest_invalid", "Runtime resource escapes its package.")
    return path


def build_manifest(root: Path) -> dict:
    root = Path(root).resolve()
    version = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8-sig"))["version"]
    files = []
    for relative in runtime_files(root):
        path = _safe_file(root, relative)
        files.append({"path": relative, "size": path.stat().st_size, "sha256": _sha(path)})
    if not _REQUIRED.issubset(item["path"] for item in files):
        raise RuntimeResourceError("plugin_runtime_path_missing", "Required runtime files are missing.")
    return {"schema_version": MANIFEST_SCHEMA, "plugin_version": version, "files": files}


def write_manifest(root: Path) -> Path:
    root = Path(root).resolve()
    path = root / MANIFEST_NAME
    path.write_bytes(_json_bytes(build_manifest(root)) + b"\n")
    return path


def _read_manifest(root: Path) -> tuple[dict, str]:
    try:
        raw = (root / MANIFEST_NAME).read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("manifest too large")
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("schema_version") != MANIFEST_SCHEMA
                or not isinstance(value.get("plugin_version"), str)
                or not isinstance(value.get("files"), list) or not 1 <= len(value["files"]) <= 20000):
            raise ValueError("invalid manifest schema")
        names = []
        for item in value["files"]:
            if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                    or type(item.get("size")) is not int or item["size"] < 0
                    or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256")))):
                raise ValueError("invalid manifest entry")
            _safe_file(root, item["path"])
            names.append(item["path"])
        if names != sorted(set(names)) or not _REQUIRED.issubset(names):
            raise ValueError("incomplete or duplicate manifest entries")
        return value, hashlib.sha256(_json_bytes(value)).hexdigest()
    except FileNotFoundError as exc:
        raise RuntimeResourceError("plugin_runtime_path_missing", "Runtime manifest is missing; reconnect to a complete version.",
                                   details={"missing_files": [MANIFEST_NAME]}) from exc
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeResourceError("plugin_runtime_manifest_invalid", "Runtime manifest is unreadable or invalid.") from exc


def assert_runtime_resources(root: Path, relative_paths=None) -> dict:
    root = Path(root).resolve()
    manifest, digest = _read_manifest(root)
    expected_digest = os.environ.get("GXP_EDITOR_RUNTIME_MANIFEST_SHA256")
    bound_root = os.environ.get("GXP_EDITOR_RUNTIME_ROOT")
    if bound_root and Path(bound_root).resolve() == root and expected_digest and digest != expected_digest:
        raise RuntimeResourceError("plugin_runtime_resource_changed", "Runtime manifest changed after instance startup.",
                                   details={"plugin_version": manifest["plugin_version"], "changed_files": [MANIFEST_NAME]})
    by_name = {item["path"]: item for item in manifest["files"]}
    selected = None if relative_paths is None else set(relative_paths)
    if selected is not None:
        prefixes = {name for name in selected if name.endswith("/")}
        selected.difference_update(prefixes)
        selected.update(name for name in by_name if any(name.startswith(prefix) for prefix in prefixes))
    missing = sorted((selected or set()) - set(by_name))
    changed = []
    for relative, item in by_name.items():
        if selected is not None and relative not in selected:
            continue
        path = _safe_file(root, relative)
        try:
            if path.stat().st_size != item["size"] or _sha(path) != item["sha256"]:
                changed.append(relative)
        except FileNotFoundError:
            missing.append(relative)
        except OSError:
            changed.append(relative)
    if selected is None:
        unexpected = sorted(set(runtime_files(root)) - set(by_name))
        changed.extend(unexpected)
        try:
            actual_version = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8-sig"))["version"]
            if actual_version != manifest["plugin_version"]:
                changed.append(".codex-plugin/plugin.json")
        except (OSError, ValueError, TypeError, KeyError):
            if ".codex-plugin/plugin.json" not in missing:
                changed.append(".codex-plugin/plugin.json")
    if missing or changed:
        code = "plugin_runtime_path_missing" if missing else "plugin_runtime_resource_changed"
        raise RuntimeResourceError(code, "Runtime resources are missing or changed; reconnect to a complete version.",
            details={"plugin_version": manifest["plugin_version"], "manifest_sha256": digest,
                     "missing_files": sorted(set(missing)), "changed_files": sorted(set(changed))})
    return {"plugin_version": manifest["plugin_version"], "manifest_sha256": digest, "runtime_root": str(root)}


def prepare_runtime(source_root: Path, storage_root=None) -> Path:
    """Verify trusted complete input, stage, verify again, then atomically activate."""
    source_root = Path(source_root).resolve()
    info = assert_runtime_resources(source_root)
    manifest, digest = _read_manifest(source_root)
    with runtime_coordination(storage_root) as storage:
        packages = storage / "packages"
        packages.mkdir(parents=True, exist_ok=True)
        target = packages / digest
        if target.exists():
            existing = assert_runtime_resources(target)
            if existing["manifest_sha256"] != digest:
                raise RuntimeResourceError("plugin_runtime_resource_changed", "Existing package identity is invalid.")
            return target
        temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=packages))
        try:
            for item in manifest["files"]:
                source = _safe_file(source_root, item["path"])
                destination = temporary / item["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            (temporary / MANIFEST_NAME).write_bytes(_json_bytes(manifest) + b"\n")
            staged = assert_runtime_resources(temporary)
            # A source modified during preparation cannot be accepted as a
            # complete trusted package, even if the copied bytes happened to fit.
            final = assert_runtime_resources(source_root)
            if staged != {**info, "runtime_root": str(temporary)} or final != info:
                raise RuntimeResourceError("plugin_runtime_resource_changed", "Runtime source changed during snapshot preparation.")
            os.replace(temporary, target)
            return target
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def python_identity() -> dict:
    executable = Path(sys.executable).resolve()
    config = Path(sys.prefix) / "pyvenv.cfg"
    siblings = []
    if os.name == "nt":
        for name in ("python.exe", "pythonw.exe"):
            sibling = executable.with_name(name)
            if sibling.is_file():
                siblings.append({"path": str(sibling), "sha256": _sha(sibling)})
    return {"path": str(executable), "version": sys.version, "prefix": str(Path(sys.prefix).resolve()),
            "executable_sha256": _sha(executable),
            "venv_config_sha256": _sha(config) if config.is_file() else None,
            "sibling_interpreters": siblings}


def _same_python_environment(actual: dict, expected: dict) -> bool:
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    if any(actual.get(key) != expected.get(key) for key in ("version", "prefix", "venv_config_sha256")):
        return False
    trusted = [{"path": expected.get("path"), "sha256": expected.get("executable_sha256")}]
    siblings = expected.get("sibling_interpreters")
    if isinstance(siblings, list):
        trusted.extend(siblings)
    return any(isinstance(item, dict) and item.get("path") == actual.get("path")
               and item.get("sha256") == actual.get("executable_sha256") for item in trusted)


def assert_python_interpreter(executable: Path, expected_identity=None) -> dict:
    """Before Popen, verify the exact frozen interpreter or approved sibling."""
    try:
        if expected_identity is None:
            frozen = os.environ.get("GXP_EDITOR_PYTHON_IDENTITY")
            expected_identity = json.loads(frozen) if frozen else python_identity()
        actual = python_identity()
        if not _same_python_environment(actual, expected_identity):
            raise ValueError("host interpreter drift")
        target = Path(executable).resolve()
        approved = [{"path": expected_identity["path"], "sha256": expected_identity["executable_sha256"]}]
        approved.extend(expected_identity.get("sibling_interpreters") or [])
        expected_hash = next((item["sha256"] for item in approved if isinstance(item, dict)
                              and Path(item.get("path", "")).resolve() == target), None)
        if not expected_hash or _sha(target) != expected_hash:
            raise ValueError("requested interpreter drift")
        return {"path": str(target), "sha256": expected_hash}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeResourceError("plugin_runtime_interpreter_changed",
            "The requested Python interpreter is missing, changed, or not bound to this instance.") from exc


def _process_identity(pid: int) -> tuple[str, str | None]:
    """Return live/dead/unknown with creation identity; PID alone is insufficient."""
    if type(pid) is not int or pid <= 0:
        return "unknown", None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ("dead" if ctypes.get_last_error() == 87 else "unknown"), None
        try:
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return "unknown", None
            if code.value != 259:
                return "dead", None
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return "unknown", None
            return "live", str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return ("dead", None) if fields[0] == "Z" else ("live", fields[19])
    except FileNotFoundError:
        return "dead", None
    except (OSError, IndexError):
        return "unknown", None


def runtime_binding(root=None) -> dict:
    root = Path(root or os.environ.get("GXP_EDITOR_RUNTIME_ROOT") or Path(__file__).resolve().parents[2]).resolve()
    manifest, digest = _read_manifest(root)
    return {"runtime_instance_id": os.environ.get("GXP_EDITOR_RUNTIME_INSTANCE_ID") or uuid.uuid4().hex,
            "plugin_version": manifest["plugin_version"], "manifest_sha256": digest,
            "runtime_root": str(root), "python_identity": python_identity()}


class RuntimeLease:
    def __init__(self, root: Path, role="mcp", storage_root=None):
        if role not in {"mcp", "ui", "observer", "host"}:
            raise ValueError("Invalid runtime role")
        self.root = Path(root).resolve()
        self.role = role
        self.storage_root = _storage(storage_root)
        self.lease_id = uuid.uuid4().hex
        self.path = self.storage_root / "leases" / (self.lease_id + ".json")
        self.binding = None

    def __enter__(self):
        with runtime_coordination(self.storage_root):
            bound_root = os.environ.get("GXP_EDITOR_RUNTIME_ROOT")
            if bound_root and Path(bound_root).resolve() != self.root:
                raise RuntimeResourceError("plugin_runtime_resource_changed",
                    "The requested package differs from this instance's fixed runtime root.")
            assert_runtime_resources(self.root)
            self.binding = runtime_binding(self.root)
            expected = os.environ.get("GXP_EDITOR_PYTHON_IDENTITY")
            if expected:
                try:
                    matches = _same_python_environment(self.binding["python_identity"], json.loads(expected))
                except (ValueError, TypeError):
                    matches = False
                if not matches:
                    raise RuntimeResourceError("plugin_runtime_interpreter_changed", "Python interpreter changed; reconnect to a complete runtime.")
            state, token = _process_identity(os.getpid())
            if state != "live" or not token:
                raise RuntimeResourceError("plugin_runtime_identity_unknown", "Cannot verify runtime process identity.")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(_json_bytes({"schema_version": 1, "lease_id": self.lease_id,
                "role": self.role, "pid": os.getpid(), "process_token": token,
                "created_at": time.time(), **self.binding}))
        return self

    def __exit__(self, *exc):
        with runtime_coordination(self.storage_root):
            self.path.unlink(missing_ok=True)


def _lease_environment_users(storage: Path, interpreter: Path) -> list[dict]:
    users = []
    for path in sorted((storage / "leases").glob("*.json")):
        try:
            value = json.loads(path.read_bytes())
            identity = value["python_identity"]
            if (not isinstance(identity, dict) or not isinstance(identity.get("path"), str)
                    or type(value.get("pid")) is not int or not isinstance(value.get("process_token"), str)):
                raise ValueError("invalid lease")
            # All interpreters in the same environment share site-packages.
            expected_prefix = str(interpreter.parent.parent.resolve())
            same_environment = (Path(identity["path"]).resolve() == interpreter
                                or os.path.normcase(str(identity.get("prefix"))) == os.path.normcase(expected_prefix))
            if not same_environment:
                continue
            state, token = _process_identity(value["pid"])
            if state == "dead" or (state == "live" and token and token != value["process_token"]):
                continue
            users.append({"lease_id": value.get("lease_id"), "role": value.get("role"), "process_state": state})
        except (OSError, ValueError, TypeError, KeyError):
            users.append({"lease_id": path.stem, "process_state": "unknown"})
    return users


def _legacy_environment_users(interpreter: Path) -> list[dict]:
    """Also protect older processes which do not participate in the lease protocol.

    Process command lines are inspected locally but never returned or logged.
    A failed inventory is unknown, rather than evidence of an unused runtime.
    """
    if os.name == "nt":
        import subprocess
        script = r'''
$targetEnvironment = $env:GXP_EDITOR_CHECK_ENVIRONMENT.TrimEnd('\')
$currentSetupPid = [int]$env:GXP_EDITOR_CHECK_PID
$users = @()
Get-CimInstance Win32_Process -Filter "Name LIKE 'python%.exe'" -ErrorAction Stop | ForEach-Object {
    if ($_.ProcessId -ne $currentSetupPid) {
        if (-not $_.ExecutablePath -or -not $_.CommandLine) {
            $users += @{pid = $_.ProcessId; process_state = 'unknown'; reason_code = 'legacy_process_unreadable'}
        } elseif ($_.ExecutablePath.StartsWith($targetEnvironment + '\', [StringComparison]::OrdinalIgnoreCase) -or
                  $_.CommandLine.IndexOf($targetEnvironment + '\', [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $users += @{pid = $_.ProcessId; process_state = 'live'; reason_code = 'legacy_environment_process'}
        }
    }
}
ConvertTo-Json -InputObject @($users) -Compress
'''
        try:
            result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                env={**os.environ, "GXP_EDITOR_CHECK_ENVIRONMENT": str(interpreter.parent.parent),
                     "GXP_EDITOR_CHECK_PID": str(os.getpid())}, capture_output=True, text=True,
                timeout=8, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            values = json.loads(result.stdout)
            if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
                raise ValueError("invalid process inventory")
            return values
        except (OSError, ValueError, subprocess.SubprocessError):
            return [{"process_state": "unknown", "reason_code": "legacy_process_inventory_unavailable"}]
    users = []
    try:
        # Linux is used for developer fixtures; the production entry is Windows.
        for folder in Path("/proc").iterdir():
            if not folder.name.isdigit() or int(folder.name) == os.getpid():
                continue
            try:
                if not (folder / "comm").read_text().strip().startswith("python"):
                    continue
                arguments = (folder / "cmdline").read_bytes().split(b"\0")
                target = str(interpreter).encode()
                if arguments and arguments[0] == target:
                    users.append({"pid": int(folder.name), "process_state": "live", "reason_code": "legacy_environment_process"})
            except FileNotFoundError:
                continue
            except OSError:
                users.append({"pid": int(folder.name), "process_state": "unknown", "reason_code": "legacy_process_unreadable"})
    except OSError:
        return [{"process_state": "unknown", "reason_code": "legacy_process_inventory_unavailable"}]
    return users


@contextlib.contextmanager
def guarded_environment_update(interpreter_path: Path, storage_root=None):
    """The caller must keep this context around the entire venv/pip mutation."""
    with runtime_coordination(storage_root) as storage:
        interpreter = Path(interpreter_path).resolve()
        users = _lease_environment_users(storage, interpreter)
        users.extend(_legacy_environment_users(interpreter))
        if users:
            raise RuntimeResourceError("plugin_runtime_environment_in_use",
                "Shared Python environment has active or unverifiable users; close them or create a separate environment.",
                details={"active_or_unknown_leases": users, "reconnect_required": False})
        yield


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "setup", "build"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--interpreter", type=Path)
    args = parser.parse_args(argv)
    if args.command == "build":
        print(write_manifest(args.source))
        return
    if args.command == "setup":
        import subprocess
        if args.interpreter is None:
            parser.error("setup requires --interpreter")
        with guarded_environment_update(args.interpreter):
            if not args.interpreter.is_file():
                subprocess.run([sys.executable, "-m", "venv", str(args.interpreter.parent.parent)], check=True)
            subprocess.run([str(args.interpreter), "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip"], check=True)
            subprocess.run([str(args.interpreter), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(args.source / "requirements.txt")], check=True)
        return
    package = prepare_runtime(args.source)
    os.environ["GXP_EDITOR_RUNTIME_ROOT"] = str(package)
    os.environ["GXP_EDITOR_RUNTIME_INSTANCE_ID"] = uuid.uuid4().hex
    os.environ["GXP_EDITOR_RUNTIME_MANIFEST_SHA256"] = _read_manifest(package)[1]
    os.environ["GXP_EDITOR_PYTHON_IDENTITY"] = json.dumps(python_identity(), sort_keys=True)
    sys.path.insert(0, str(package / "mcp"))
    # Do not let a package imported for bootstrapping keep deferred imports tied
    # to the host-managed source directory after it disappears.
    for name in list(sys.modules):
        if name == "gxp_editor" or name.startswith("gxp_editor."):
            del sys.modules[name]
    with RuntimeLease(package, role="mcp"):
        runpy.run_path(str(package / "mcp/server.py"), run_name="__main__")


if __name__ == "__main__":
    try:
        _main()
    except RuntimeResourceError as exc:
        print(json.dumps({"error_code": exc.code, "message": str(exc), "error_details": exc.details}), file=sys.stderr)
        raise SystemExit(2)
