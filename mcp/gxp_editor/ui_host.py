"""Explicitly started, local UI host outside the MCP client's teardown tree.

The MCP server never starts this host. It only submits fixed UI entrypoints.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

from .config import runtime_root
from .execution_lock import _os_lock
from .ui_lifecycle import atomic_json, in_windows_job, process_identity, read_json

_ID = re.compile(r"^[0-9a-f]{32}$")


def _private_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import win32api
        import win32con
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        acl = win32security.ACL()
        flags = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
        for principal in (sid, win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None),
                          win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)):
            acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, flags, 0x1F01FF, principal)
        win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
    else:
        path.chmod(0o700)


def _contained(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    return path == root or root in path.parents


def validate_request(request, *, package_root, startup_root, fixture_script=None):
    """No arbitrary command, executable, working directory, or business SQL IPC."""
    if not isinstance(request, dict) or not _ID.fullmatch(str(request.get("startup_id", ""))):
        raise ValueError("Invalid UI startup identity")
    if any(not isinstance(request.get(k), str) for k in ("cwd", "record_path", "executable", "script")):
        raise ValueError("Invalid UI entrypoint path")
    if not isinstance(request.get("environment", {}), dict):
        raise ValueError("Invalid UI environment binding")
    package_root, startup_root = Path(package_root).resolve(), Path(startup_root).resolve()
    if Path(request.get("cwd", "")).resolve() != package_root:
        raise ValueError("UI package root does not match the host")
    record = Path(request.get("record_path", "")).resolve()
    if record != startup_root / "attempts" / (request["startup_id"] + ".json"):
        raise ValueError("UI receipt path is outside the host scope")
    snapshot = read_json(record)
    if not snapshot or snapshot.get("startup_id") != request["startup_id"]:
        raise ValueError("UI startup receipt changed")
    if snapshot.get("status") != "starting":
        raise ValueError("UI startup request is no longer pending")
    if Path(snapshot.get("log_path", "")).resolve() != startup_root / "logs" / (request["startup_id"] + ".log"):
        raise ValueError("UI log path is outside the host scope")
    executable, actual = Path(request.get("executable", "")).resolve(), Path(sys.executable).resolve()
    if executable not in {actual, actual.with_name("pythonw.exe"), actual.with_name("python.exe")}:
        raise ValueError("UI interpreter does not match the host environment")
    script = Path(request.get("script", "")).resolve()
    args = request.get("arguments")
    if not isinstance(args, list) or any(not isinstance(a, str) or len(a) > 2048 for a in args):
        raise ValueError("Invalid UI arguments")
    if fixture_script is not None:
        if script != Path(fixture_script).resolve() or args:
            raise ValueError("Fixture host only accepts its one predeclared stub")
    elif script == package_root / "scripts" / "approval_app.py":
        if len(args) == 3 and args[0] == "--queue":
            valid = all(_ID.fullmatch(a) for a in args[1:])
        elif len(args) == 2 and args[0] == "--plan-review":
            valid = _contained(args[1], runtime_root()) and Path(args[1]).suffix == ".json"
        else:
            valid = len(args) in {1, 3} and bool(_ID.fullmatch(args[0]))
            if len(args) == 3:
                valid = valid and (not args[1] or bool(_ID.fullmatch(args[1]))) and args[2].isdigit() and len(args[2]) < 12
        if not valid:
            raise ValueError("Invalid fixed approval entrypoint arguments")
    elif script == package_root / "scripts" / "abort_session_app.py":
        if not (len(args) == 3 and _ID.fullmatch(args[0]) and (not args[1] or _ID.fullmatch(args[1]))
                and args[2].isdigit() and len(args[2]) < 12):
            raise ValueError("Invalid fixed recovery entrypoint arguments")
    else:
        raise ValueError("UI host only accepts registered fixed entrypoints")
    if fixture_script is None:
        from .runtime_package import assert_python_interpreter, runtime_binding
        environment = request.get("environment", {})
        binding = runtime_binding(package_root)
        if (Path(environment.get("GXP_EDITOR_RUNTIME_ROOT", "")).resolve() != package_root
                or environment.get("GXP_EDITOR_RUNTIME_MANIFEST_SHA256") != binding["manifest_sha256"]):
            raise ValueError("UI request runtime binding differs from this host")
        frozen = json.loads(environment.get("GXP_EDITOR_PYTHON_IDENTITY", "null"))
        if not isinstance(frozen, dict):
            raise ValueError("UI request has no frozen interpreter identity")
        assert_python_interpreter(executable, frozen)
    return {**request, "fixture": fixture_script is not None,
            "maximum_seconds": 15 if fixture_script is not None else 7 * 24 * 3600,
            "environment": {k: v for k, v in request.get("environment", {}).items()
                if k in {"GXP_EDITOR_RUNTIME_ROOT", "GXP_EDITOR_RUNTIME_INSTANCE_ID", "GXP_EDITOR_RUNTIME_MANIFEST_SHA256",
                         "GXP_EDITOR_PYTHON_IDENTITY", "GXP_LOWCODE_EDITOR_STATE", "GXP_LOWCODE_EDITOR_CONFIG",
                         "GXP_EDITOR_QUEUE_LAUNCH_TOKEN", "GXP_EDITOR_QUEUE_GENERATION"}
                and isinstance(v, str) and len(v) <= 4096}}


def host_status(startup_root):
    info = read_json(Path(startup_root) / "host" / "host.json") or {}
    state, token = process_identity(info.get("pid"))
    if state != "alive" or not token or token != info.get("process_token"):
        return None
    return info


def submit_request(request, startup_root):
    host = host_status(startup_root)
    if not host:
        return False
    if host.get("in_job") is not False and not host.get("fixture_only"):
        return False
    if not host.get("fixture_only") and in_windows_job(host["pid"]) is not False:
        return False
    if Path(host.get("runtime_root", "")).resolve() != Path(request["cwd"]).resolve():
        return False
    request = {**request, "host_nonce": host["nonce"], "isolation_mode": "external_host"}
    atomic_json(Path(startup_root) / "host" / "requests" / (request["startup_id"] + ".json"), request)
    return True


def run_host(package_root, startup_root, *, maximum_seconds=None, fixture_script=None):
    package_root, startup_root = Path(package_root).resolve(), Path(startup_root).resolve()
    directory = startup_root / "host"
    _private_directory(directory)
    requests = directory / "requests"
    requests.mkdir(exist_ok=True)
    lease = nullcontext()
    if fixture_script is None:
        from .runtime_package import RuntimeLease, assert_runtime_resources
        assert_runtime_resources(package_root)
        if in_windows_job() is not False:
            raise RuntimeError("ui_host_isolation_unverified: start this host from a separate user terminal outside the MCP client's Job")
        from .runtime_package import runtime_binding
        binding = runtime_binding(package_root)
        os.environ.update({"GXP_EDITOR_RUNTIME_ROOT": str(package_root),
            "GXP_EDITOR_RUNTIME_INSTANCE_ID": binding["runtime_instance_id"],
            "GXP_EDITOR_RUNTIME_MANIFEST_SHA256": binding["manifest_sha256"],
            "GXP_EDITOR_PYTHON_IDENTITY": json.dumps(binding["python_identity"], sort_keys=True)})
        lease = RuntimeLease(package_root, role="host")
    with lease, _os_lock(directory / "host", ".lock", RuntimeError, "A UI host is already running"):
        nonce = secrets.token_hex(32)
        atomic_json(directory / "host.json", {"schema_version": 1, "pid": os.getpid(),
            "process_token": process_identity(os.getpid())[1], "parent_pid": os.getppid(),
            "created_at": time.time(), "runtime_root": str(package_root), "nonce": nonce,
            "in_job": in_windows_job(), "fixture_only": fixture_script is not None,
            "launch_provenance": "explicit_external_entrypoint"})
        children = []
        accepted_count = 0
        deadline = time.monotonic() + maximum_seconds if maximum_seconds else None
        try:
            while deadline is None or time.monotonic() < deadline:
                for source in requests.glob("*.json"):
                    if source.stat().st_size > 16384:
                        source.unlink()
                        continue
                    request = read_json(source) or {}
                    if not hmac.compare_digest(str(request.pop("host_nonce", "")), nonce):
                        source.unlink()
                        continue
                    try:
                        accepted = validate_request(request, package_root=package_root,
                            startup_root=startup_root, fixture_script=fixture_script)
                        accepted_path = directory / "accepted" / source.name
                        if accepted_path.exists():
                            continue  # A consumed startup ID is never replayed.
                        atomic_json(accepted_path, accepted)
                        # Observer owns launcher + real UI handles independently
                        # of the MCP connection. No process-tree teardown here.
                        log_path = (read_json(Path(accepted["record_path"])) or {})["log_path"]
                        with Path(log_path).open("ab") as stderr:
                            process = subprocess.Popen([sys.executable, str(package_root / "scripts" / "ui_observer.py"), str(accepted_path)],
                                cwd=package_root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=stderr, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        children.append(process)
                        accepted_count += 1
                    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                        startup_id = request.get("startup_id", "")
                        if isinstance(startup_id, str) and _ID.fullmatch(startup_id):
                            from .ui_lifecycle import lifecycle_event
                            lifecycle_event(startup_root / "attempts" / (startup_id + ".json"), startup_id,
                                "host_request_rejected", source="host", updates={"status": "failed",
                                    "process_state": "exited",
                                    "reason_code": getattr(exc, "code", "ui_host_request_rejected"),
                                    "error": "UI host rejected a changed or invalid request"})
                    finally:
                        source.unlink(missing_ok=True)
                children = [p for p in children if p.poll() is None]
                if fixture_script is not None and accepted_count and not children:
                    break
                time.sleep(.025)
        finally:
            (directory / "host.json").unlink(missing_ok=True)
            # Fixtures have a finite lifetime; production observers continue
            # independently if the user closes this host console.
            if fixture_script is not None:
                for child in children:
                    child.wait(timeout=18)


def main():
    parser = argparse.ArgumentParser(description="Explicit local UI host; start outside the MCP client before reconnecting")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--startup-root")
    parser.add_argument("--maximum-seconds", type=float)
    args = parser.parse_args()
    run_host(args.runtime_root, args.startup_root or runtime_root() / "ui-startup",
             maximum_seconds=args.maximum_seconds)
    return 0
