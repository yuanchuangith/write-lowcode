"""Current-generation metadata collectors. Never reads business table rows."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from gxp_project_context import atomic_json, pin_context, project_operation


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("PROJECT_REFRESH_DEADLINE")
    return value


def _database(ctx):
    from .config import DatabaseConfig
    from .db import ReadOnlyDatabase
    concurrency = max(1, int(ctx.platform_version.get("refresh", {}).get("database_concurrency", 2)))
    return ReadOnlyDatabase(DatabaseConfig.from_dict({**ctx.database_configs["look"], "pool_size": concurrency,
        "read_timeout_seconds": max(30, int(ctx.database_configs["look"].get("read_timeout_seconds", 10)))}))


def collect_schema(ctx, deadline):
    from .schema_repository import SchemaMetadataRepository
    schema = SchemaMetadataRepository(_database(ctx)).load_schema(deadline=deadline)
    fingerprint = hashlib.sha256(json.dumps(schema, sort_keys=True, ensure_ascii=False, default=str,
                                            separators=(",", ":")).encode()).hexdigest()
    root = Path(ctx.snapshot_root("schema"))
    root.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".s-", dir=root.parent))
    tables, declared = [], 0
    try:
        for table in schema.get("tables", []):
            _remaining(deadline)
            name = str(table["table_name"])
            relative = "tables/" + hashlib.sha256(name.encode()).hexdigest()[:24] + ".json"
            atomic_json(work / relative, table)
            tables.append({"table_name": name, "table_comment": table.get("table_comment"),
                           "table_type": table.get("table_type"), "file": relative,
                           "columns": [column.get("column_name") for column in table.get("columns", [])]})
            declared += len(table.get("foreign_keys", []))
        # Existing readers accept these standard files; no data-relation aggregates are run.
        _json_value(work / "indexes/tables.json", tables)
        _json_value(work / "relations/verified.json", [])
        _json_value(work / "validation-attempts.json", [])
        atomic_json(work / "indexes/table-usage.json", {"generated_at": _now(), "usages": [], "note": "metadata only"})
        counts = {"tables": len(tables), "declared_foreign_keys": declared, "candidates": 0, "verified_relations": 0}
        atomic_json(work / "manifest.json", {"version": 1, "database": schema.get("database"), "completed_at": _now(),
            "schema_fingerprint": fingerprint, "project_binding": ctx.binding(), "database_scope": ctx.database_scope,
            "refresh_generation": ctx.refresh_generation, "metadata_only": True, "counts": counts,
            "policy": {"scope_id": ctx.business_scope_id, "available": False, "storage": "local", "error": "metadata_only"}})
        backup = root.with_name("." + root.name + ".previous")
        if backup.exists():
            shutil.rmtree(backup)
        if root.exists():
            os.replace(root, backup)
        try:
            os.replace(work, root)
        except Exception:
            if backup.exists() and not root.exists():
                os.replace(backup, root)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if work.exists():
            shutil.rmtree(work)
    return {"status": "ready", "complete": True, "metadata_only": True,
            "schema_fingerprint": fingerprint, "counts": counts,
            "snapshot_dir": str(ctx.snapshot_root("schema")), "completed_at": _now()}


def _json_value(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, default=str), encoding="utf-8")


def collect_actions(ctx, deadline, progress=None):
    from .repository import GxpRepository
    db = _database(ctx)
    repo = GxpRepository(db)
    actions, designs = [], []
    # Both catalogs are platform configuration metadata, not application rows.
    for table, kind, name in (("cpm_public_flows", "public", "name"), ("cpm_bizflows", "form", "describe")):
        after = ""
        while True:
            _remaining(deadline)
            page = ", page_id" if kind == "form" else ", NULL AS page_id"
            sql = f"SELECT id AS ref_id, code AS action_code, `{name}` AS action_name{page} FROM `{table}` WHERE isDeleted=0 AND id>%(after)s ORDER BY id LIMIT 501"
            with db.session(timeout_ms=min(10000, int(_remaining(deadline) * 1000))) as session:
                rows, truncated = session.query(sql, {"after": after}, max_rows=500)
            actions.extend({**row, "action_type": kind} for row in rows)
            if not rows or not truncated:
                break
            next_id = str(rows[-1]["ref_id"])
            if next_id <= after:
                raise ValueError("ACTION_CATALOG_PAGINATION_INVALID")
            after = next_id
    selected, aliases = repo._design_select(include_content=False, include_hashes=False)
    metadata_fields = ("design_id", "ref_id", "is_publish", "is_deleted", "modified_time", "created_time")
    selected = ", ".join(f"`{aliases[key]}` AS `{key}`" for key in metadata_fields if aliases.get(key))
    id_column, deleted_column = aliases.get("design_id"), aliases["is_deleted"]
    if not id_column:
        raise ValueError("ACTION_DESIGN_KEY_REQUIRED")
    after = ""
    design_ids = []
    while True:
        _remaining(deadline)
        sql = (f"SELECT `{id_column}` AS design_id FROM cpm_bizflows_design "
               f"WHERE `{deleted_column}`=0 AND `{id_column}`>%(after)s ORDER BY `{id_column}` LIMIT 501")
        with db.session(timeout_ms=min(10000, int(_remaining(deadline) * 1000))) as session:
            rows, truncated = session.query(sql, {"after": after}, max_rows=500)
        design_ids.extend(str(row["design_id"]) for row in rows)
        if not rows or not truncated:
            break
        next_id = str(rows[-1]["design_id"])
        if next_id <= after:
            raise ValueError("ACTION_DESIGN_PAGINATION_INVALID")
        after = next_id
    metadata_batch_size = 4

    def metadata_batch(ids):
        with pin_context(ctx):
            params = {"id" + str(index): value for index, value in enumerate(ids)}
            placeholders = ",".join("%(" + name + ")s" for name in params)
            sql = (f"SELECT {selected} FROM cpm_bizflows_design WHERE `{deleted_column}`=0 "
                   f"AND `{id_column}` IN ({placeholders}) LIMIT {len(ids) + 1}")
            try:
                with db.session(timeout_ms=min(10000, int(_remaining(deadline) * 1000))) as session:
                    rows, truncated = session.query(sql, params, max_rows=len(ids))
            except Exception as error:
                error.collector_phase = "design_metadata"
                raise
            if truncated or {str(row["design_id"]) for row in rows} != set(ids):
                raise ValueError("ACTION_DESIGN_CHANGED_DURING_REFRESH")
            return [repo._design_row(row, include_content=False) for row in rows]
    batches = [design_ids[offset:offset + metadata_batch_size]
               for offset in range(0, len(design_ids), metadata_batch_size)]
    workers = max(1, min(2, int(ctx.platform_version.get("refresh", {}).get("database_concurrency", 2))))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for rows in executor.map(metadata_batch, batches):
            designs.extend(rows)
            if progress:
                progress({"status": "fetching", "stage": "design_metadata",
                          "counts": {"designs_read": len(designs), "designs_total": len(design_ids)}})
    current = {}
    for row in designs:
        key = str(row["ref_id"]) + ":" + row["version"]
        previous = current.get(key)
        order = lambda value: (str(value.get("modified_time") or value.get("created_time") or ""), str(value.get("design_id") or ""))
        if previous is None or order(row) > order(previous):
            current[key] = row
    # Select current metadata before hashing. Historical large designs do not
    # need to be read and hashed again merely to discover the current version.
    selected, _ = repo._design_select(include_content=False, include_hashes=True)
    targets = list(current.items())
    for offset in range(0, len(targets), 4):
        _remaining(deadline)
        batch = targets[offset:offset + 4]
        params = {"id" + str(index): row["design_id"] for index, (_, row) in enumerate(batch)}
        placeholders = ",".join("%(" + name + ")s" for name in params)
        sql = f"SELECT {selected} FROM cpm_bizflows_design WHERE `{deleted_column}`=0 AND `{id_column}` IN ({placeholders}) LIMIT 5"
        try:
            with db.session(timeout_ms=min(10000, int(_remaining(deadline) * 1000))) as session:
                rows, truncated = session.query(sql, params, max_rows=4)
        except Exception as error:
            error.collector_phase = "current_design_fingerprint"
            raise
        actual = {str(row["design_id"]): repo._design_row(row, include_content=False) for row in rows}
        if truncated or len(rows) != len(batch):
            raise ValueError("ACTION_DESIGN_CHANGED_DURING_REFRESH")
        for key, baseline in batch:
            row = actual.get(str(baseline["design_id"]))
            if row is None or any(row.get(field) != baseline.get(field) for field in
                                  ("ref_id", "version", "modified_time", "created_time")):
                raise ValueError("ACTION_DESIGN_CHANGED_DURING_REFRESH")
            current[key] = row
        if progress:
            progress({"status": "fetching", "stage": "current_design_fingerprint",
                      "counts": {"fingerprints_checked": min(offset + 4, len(targets)), "current_designs": len(targets)}})
    # Normalize datetime/decimal metadata without retaining raw design/source text.
    payload = json.loads(json.dumps({"project_binding": ctx.binding(), "refresh_generation": ctx.refresh_generation,
        "database_scope": ctx.database_scope, "actions": actions, "designs": list(current.values()), "completed_at": _now()}, default=str))
    root = Path(ctx.snapshot_root("actions"))
    atomic_json(root / "catalog.json", payload)
    current = {str(row["ref_id"]) + ":" + row["version"]: row for row in payload["designs"]}
    objects = {str(row["ref_id"]): {"status": "ready", "versions": {version: current.get(str(row["ref_id"]) + ":" + version) for version in ("draft", "published")}} for row in actions}
    return {"status": "ready", "complete": True, "counts": {"actions": len(actions), "designs": len(current)},
            "objects": objects, "fingerprint": hashlib.sha256(json.dumps({"actions": payload["actions"], "designs": payload["designs"]}, sort_keys=True).encode()).hexdigest(),
            "completed_at": payload["completed_at"]}


def _reusable_committed_cpm(ctx, config, manager, manifest):
    if (not isinstance(manifest, dict)
            or manifest.get("project_binding") != ctx.binding()
            or manifest.get("refresh_generation") != ctx.refresh_generation
            or manifest.get("database_scope") != ctx.database_scope
            or (manifest.get("platform") or {}).get("url") != config.platform_url
            or (manifest.get("platform") or {}).get("appId") != config.app_id):
        return None
    status = manager.snapshot_status().get("refresh_status") or {}
    full = status.get("full") or {}
    report = full.get("report") or {}
    failures = report.get("failures") or []
    if (full.get("error") is not None or report.get("ok") is not True
            or not full.get("last_completed_at")
            or Path(report.get("outDir", "")).resolve() != Path(config.snapshot_dir).resolve()
            or report.get("counts") != manifest.get("counts")
            or report.get("health") != manifest.get("health")
            or failures != manifest.get("failures")
            or any(not isinstance(item, dict) or item.get("type") != "pages-deleted" for item in failures)):
        return None
    return {**report, "refreshed_at": full["last_completed_at"], "cli_version": status.get("cli_version")}


def collect_cpm(ctx, deadline):
    from .cpm_config import load_cpm_config
    from .cpm_runner import CpmRefreshManager
    base = load_cpm_config(database_config=ctx.database_configs["look"])
    binding = ctx.platform_binding
    if not all(binding.get(key) for key in ("platform_url", "account", "app_id", "site_out_id")):
        return {"status": "unavailable", "complete": False, "error_code": "PROJECT_PLATFORM_NOT_CONFIGURED", "objects": {}, "resources": {}}
    config = replace(base, platform_url=binding["platform_url"], account=binding["account"],
        credential_target=binding.get("credential_target") or base.credential_target,
        app_id=binding["app_id"], site_out_id=binding["site_out_id"],
        api_prefix=binding.get("form_config", {}).get("api_prefix") or "/gxp2",
        login_url=binding.get("login_url") or "",
        snapshot_dir=str(ctx.snapshot_root("cpm")), cache_scope=ctx.database_scope, fresh_pull=True,
        timeout_seconds=max(1, int(_remaining(deadline))),
        concurrency=max(1, int(ctx.platform_version.get("refresh", {}).get("http_concurrency", 4))))
    manager = CpmRefreshManager(config)
    root = Path(config.snapshot_dir)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    result = _reusable_committed_cpm(ctx, config, manager, manifest)
    if result is None:
        result = manager.refresh(force=True)
    if not result.get("ok"):
        error = result.get("error") or {}
        return {"status": "unavailable", "complete": False,
                "error_code": error.get("code", "CPM_REFRESH_FAILED"),
                "reason": str(error.get("message") or "")[:1000],
                "failures": result.get("failures") or [], "objects": {}, "resources": {}}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    failures = result.get("failures") or []
    health = result.get("health") or manifest.get("health") or {}
    page_health = health.get("pages") if isinstance(health, dict) else None
    page_counts = tuple(page_health.get(key) for key in ("total", "ok", "degraded", "deleted")) if isinstance(page_health, dict) else ()
    health_accounts_for_pages = (
        len(page_counts) == 4
        and all(type(value) is int and value >= 0 for value in page_counts)
        and page_counts[1] + page_counts[2] + page_counts[3] == page_counts[0]
        and page_counts[2] == 0
    )
    blocking_failures = [failure for failure in failures
                         if not isinstance(failure, dict) or failure.get("type") != "pages-deleted"]
    complete = (result.get("complete") is True or manifest.get("complete") is True or health_accounts_for_pages) \
        and not blocking_failures
    objects = result.get("objects") or manifest.get("objects") or {}
    if not objects:
        for path in sorted((root / "pages").glob("*/page-meta.json")):
            meta = json.loads(path.read_text(encoding="utf-8"))
            source = meta.get("designSource") or {}
            ready = source.get("version") == "draft" and source.get("appId") == config.app_id and source.get("siteOutId") == config.site_out_id
            objects["page:" + str(meta.get("route"))] = {"kind": "page", "id": meta.get("route"),
                "page_id": meta.get("outId") or meta.get("id"), "status": "ready" if ready else "unavailable",
                "logic_complete": ready and not failures}
    state = "ready" if complete else "partial" if objects else "incomplete"
    manifest.update(project_binding=ctx.binding(), database_scope=ctx.database_scope, refresh_generation=ctx.refresh_generation,
                    complete=complete, status=state, objects=objects)
    atomic_json(manifest_path, manifest)
    return {"status": state, "complete": complete, "objects": objects,
            "resources": result.get("resources") or manifest.get("resources") or {}, "failures": failures,
            "refreshed_at": result.get("refreshed_at"), "error_code": None if complete else "CPM_COMPLETENESS_UNVERIFIED"}


def run_collectors(ctx, report_callback=None, resource_names=None):
    """Collect selected resource classes; omitted classes are left untouched."""
    names = {"schema", "actions", "cpm"} if resource_names is None else set(resource_names)
    if names - {"schema", "actions", "cpm"}:
        raise ValueError("PROJECT_RESOURCE_INVALID")
    budget = max(1, int(ctx.platform_version.get("refresh", {}).get("timeout_seconds", 900)))
    started_at, deadline = _now(), time.monotonic() + budget
    resources = {}

    def run(name, fn):
        with pin_context(ctx):
            if report_callback:
                report_callback(name, {"status": "fetching", "started_at": _now()})
            try:
                if name == "actions":
                    value = fn(ctx, deadline, progress=(lambda result: report_callback(name, result)) if report_callback else None)
                else:
                    value = fn(ctx, deadline)
            except Exception as exc:
                # Configuration endpoints and host details never enter job errors.
                value = {"status": "unavailable", "complete": False, "error_code": type(exc).__name__}
                if getattr(exc, "collector_phase", None) in {"design_metadata", "current_design_fingerprint"}:
                    value["stage"] = exc.collector_phase
                cause = exc
                while cause is not None:
                    if cause.args and type(cause.args[0]) is int:
                        value["database_error_code"] = cause.args[0]
                        if cause.args[0] in {2013, 3024}:
                            value["reason"] = "数据库读取超时，请检查目标库响应速度。"
                        break
                    cause = cause.__cause__
            resources[name] = value
            if report_callback:
                report_callback(name, value)
            return value

    # Schema + action metadata never exceed two database connections; HTTP is independent.
    with ThreadPoolExecutor(max_workers=2) as executor:
        cpm = executor.submit(run, "cpm", collect_cpm) if "cpm" in names else None
        if "schema" in names:
            run("schema", collect_schema)
        if "actions" in names:
            run("actions", collect_actions)
        if cpm is not None:
            cpm.result()
    with pin_context(ctx):
        try:
            from .business_graph_store import BusinessGraphStore
            dependencies = [{"kind": "action", "key": ref, "version": obj["versions"]["published"]["design_id"]}
                for ref, obj in resources.get("actions", {}).get("objects", {}).items() if obj.get("versions", {}).get("published")]
            for ref, obj in resources.get("actions", {}).get("objects", {}).items():
                published = obj.get("versions", {}).get("published")
                if published:
                    version = hashlib.sha256(json.dumps([published.get("design_id"), published.get("data_sha256"),
                                                        published.get("csharp_sha256")], separators=(",", ":")).encode()).hexdigest()
                    dependencies.append({"kind": "action_content", "key": ref, "version": version})
            fingerprint = resources.get("schema", {}).get("schema_fingerprint")
            if fingerprint:
                dependencies.append({"kind": "catalog", "key": "schema-" + ctx.business_scope_id, "version": fingerprint})
            cpm = resources.get("cpm", {})
            if cpm.get("complete") is True and cpm.get("content_fingerprint"):
                dependencies.append({"kind": "catalog", "key": "cpm-" + ctx.business_scope_id, "version": cpm["content_fingerprint"]})
            if dependencies:
                with project_operation(expected_binding=ctx.binding(), role="look"):
                    BusinessGraphStore(context=ctx).update_dependency_versions(dependencies)
        except Exception:
            # Historical graphs are optional hints; inability to refresh them cannot fabricate readiness.
            pass
    return {"resources": resources, "started_at": started_at, "completed_at": _now()}
