"""Durable, project-pinned full refresh jobs; a failed pull never undoes activation."""
from __future__ import annotations

import argparse
import copy
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .context import (
    ProjectContext, ProjectError, active_path, assert_active, atomic_json,
    get_context, operation_gate, pin_context, read_json, runtime_root,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _manifest_path(ctx):
    return ctx.snapshot_root("status").parent / "manifest.json"


def initialize_refresh(ctx: ProjectContext, platform_probe=None, retry_resources=None):
    manifest = {"schema_version": 2, "job_id": ctx.refresh_generation,
                "project_binding": ctx.binding(), "project_id": ctx.project_id,
                "activation_id": ctx.activation_id, "refresh_generation": ctx.refresh_generation,
                "profile_revision": ctx.profile_revision, "status": "pending", "created_at": _now(),
                "platform_probe": platform_probe or {}, "resources": {name: {"status": "pending"} for name in ("schema", "actions", "cpm")},
                "retry_resources": retry_resources or []}
    atomic_json(_manifest_path(ctx), manifest)
    atomic_json(runtime_root() / "jobs" / (ctx.refresh_generation + ".json"),
                {"context": ctx.to_dict(), "job_id": ctx.refresh_generation})
    return manifest


def _save(ctx, manifest):
    # The path is permanently pinned to this generation, even if another project
    # has become active. No mutable global "latest" file can be overwritten.
    atomic_json(_manifest_path(ctx), manifest)


def _missing(manifest):
    result = []
    for resource, status in manifest.get("resources", {}).items():
        if status.get("status") != "ready":
            result.append({"resource": resource, "status": status.get("status", "pending"),
                           "error_code": status.get("error_code"), "failures": status.get("failures", [])[:100]})
    return result


def launch_refresh(ctx):
    python = Path(sys.executable)
    if os.name == "nt" and python.with_name("pythonw.exe").exists():
        python = python.with_name("pythonw.exe")
    try:
        package_root = str(Path(__file__).resolve().parent.parent)
        env = {**os.environ, "PYTHONPATH": package_root + os.pathsep + os.environ.get("PYTHONPATH", "")}
        process = subprocess.Popen([str(python), "-m", "gxp_project_context.refresh", "--job", ctx.refresh_generation],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS} if os.name == "nt" else {"start_new_session": True}))
        return {"status": "pending", "job_id": ctx.refresh_generation, "process_id": process.pid}
    except Exception:
        manifest = read_json(_manifest_path(ctx)) or {}
        manifest.update(status="unavailable", error_code="PROJECT_REFRESH_START_FAILED", completed_at=_now())
        manifest["resources"] = {name: {"status": "failed", "error_code": "PROJECT_REFRESH_START_FAILED"} for name in ("schema", "actions", "cpm")}
        _save(ctx, manifest)
        return {"status": "unavailable", "job_id": ctx.refresh_generation, "error_code": "PROJECT_REFRESH_START_FAILED"}


def run_job(ctx: ProjectContext, collector=None):
    """Run at most one collector for a generation, with a bounded parent watchdog."""
    manifest = read_json(_manifest_path(ctx)) or initialize_refresh(ctx)
    # A generation is a new independent pull, not a fallback overlay on history.
    lock_path = ctx.snapshot_root("status").parent / "refresh.lock"
    from .job_lock import job_lock
    with job_lock(lock_path):
        manifest = read_json(_manifest_path(ctx)) or manifest
        if manifest.get("status") in {"ready", "partial", "unavailable"} and manifest.get("completed_at"):
            return manifest
        manifest.update(status="fetching", started_at=_now(), process_id=os.getpid())
        _save(ctx, manifest)
        write_lock = threading.Lock()
        completed = threading.Event()
        budget = int(ctx.platform_version.get("refresh", {}).get("timeout_seconds", 900))

        def report(resource, result):
            with write_lock:
                if completed.is_set():
                    return
                manifest.setdefault("resources", {})[resource] = result
                _save(ctx, manifest)

        def collect():
            try:
                selected = collector
                if selected is None:
                    from ._collectors.project_refresh import run_collectors
                    selected = run_collectors
                with pin_context(ctx):
                    if collector is None:
                        result = selected(ctx, report, resource_names=manifest.get("retry_resources") or None)
                    else:
                        result = selected(ctx, report)
                with write_lock:
                    if not completed.is_set() and isinstance(result, dict):
                        manifest["resources"].update(result.get("resources", {}))
            except Exception:
                with write_lock:
                    if not completed.is_set():
                        for name in ("schema", "actions", "cpm"):
                            if manifest["resources"].get(name, {}).get("status") in {"pending", "fetching"}:
                                manifest["resources"][name] = {"status": "failed", "error_code": "PROJECT_COLLECTOR_FAILED"}
            finally:
                completed.set()

        worker = threading.Thread(target=collect, name="gxp-project-refresh", daemon=True)
        worker.start()
        finished = completed.wait(timeout=budget)
        with write_lock:
            completed.set()
            for name in ("schema", "actions", "cpm"):
                if manifest["resources"].get(name, {}).get("status") in {"pending", "fetching"}:
                    manifest["resources"][name] = {"status": "failed", "error_code": "PROJECT_REFRESH_TIMEOUT" if not finished else "PROJECT_RESOURCE_INCOMPLETE"}
            states = [item.get("status") for item in manifest["resources"].values()]
            available = any(state in {"ready", "partial", "incomplete"} for state in states)
            manifest.update(status="ready" if all(state == "ready" for state in states) else "partial" if available else "unavailable",
                            completed_at=_now(), missing_resources=_missing(manifest))
            try:
                assert_active(ctx)
                manifest["active_generation"] = True
            except ProjectError:
                manifest["active_generation"] = False
            _save(ctx, manifest)
        return manifest


def refresh_status(job_id: str = ""):
    import re
    if not job_id:
        ctx = get_context()
        if not ctx or not ctx.managed:
            return {"status": "not_started", "resources": {}}
        job_id = ctx.refresh_generation
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ProjectError("PROJECT_JOB_INVALID", "刷新任务标识无效。")
    job = read_json(runtime_root() / "jobs" / (job_id + ".json"))
    if not job:
        return {"status": "unavailable", "job_id": job_id, "error_code": "PROJECT_JOB_NOT_FOUND"}
    ctx = ProjectContext.from_dict(job["context"])
    manifest = read_json(_manifest_path(ctx), max_bytes=16 * 1024 * 1024) or {}
    manifest["missing_resources"] = _missing(manifest)
    if manifest.get("status") in {"fetching", "pending"}:
        started = manifest.get("started_at") or manifest.get("created_at")
        try:
            expired = time.time() - datetime.fromisoformat(started).timestamp() > int(ctx.platform_version.get("refresh", {}).get("timeout_seconds", 900)) + 30
        except (ValueError, TypeError):
            expired = True
        if expired:
            manifest = {**manifest, "status": "unavailable", "error_code": "PROJECT_REFRESH_INTERRUPTED"}
    return manifest


def refresh_project(expected_revision: str, *, retry_failed=False):
    from .settings import _check_revision, active_sessions, revision, publish_metadata
    from .context import project_active_settings
    with operation_gate():
        _check_revision(expected_revision)
        ctx = get_context(required=True)
        if not ctx.managed:
            raise ProjectError("PROJECT_NOT_MANAGED", "请先保存并选择项目配置。")
        old_status = refresh_status(ctx.refresh_generation)
        if old_status.get("status") in {"pending", "fetching"}:
            return {"ok": True, "changed": False, "job_id": ctx.refresh_generation, "refresh": old_status}
        assert_active(ctx)
        # Refresh preserves activation and immutable editor baselines.
        failed = [name for name, item in old_status.get("resources", {}).items() if item.get("status") != "ready"] if retry_failed else []
        if retry_failed and not failed:
            return {"ok": True, "changed": False, "revision": revision(), "job_id": ctx.refresh_generation, "refresh": old_status}
        if retry_failed:
            new_ctx = ctx
            manifest = read_json(_manifest_path(ctx)) or {}
            for resource in failed:
                manifest.setdefault("resources", {})[resource] = {"status": "pending"}
            manifest.update(status="pending", retry_resources=failed, created_at=_now())
            for key in ("completed_at", "started_at", "error_code"):
                manifest.pop(key, None)
            _save(ctx, manifest)
        else:
            new_ctx = ProjectContext.from_dict({**ctx.to_dict(), "refresh_generation": secrets.token_hex(16)})
            initialize_refresh(new_ctx)
            pointer = project_active_settings()
            pointer.update(refresh_generation=new_ctx.refresh_generation, refresh_state="pending", revision=secrets.token_hex(16))
            publish_metadata(pointer)
    return {"ok": True, "changed": True, "revision": revision(), "job_id": new_ctx.refresh_generation,
            "refresh": launch_refresh(new_ctx)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    import re
    if not re.fullmatch(r"[a-f0-9]{32}", args.job):
        return 2
    job = read_json(runtime_root() / "jobs" / (args.job + ".json"))
    if not job:
        return 2
    run_job(ProjectContext.from_dict(job["context"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
