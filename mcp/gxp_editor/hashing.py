from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="milliseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        if value in (b"\x00", b"\x01"):
            return int.from_bytes(value, "big")
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=json_default,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def compiler_binding_payload(binding: dict[str, Any]) -> dict[str, Any]:
    """Canonical fields shared by compiler fingerprints and approval validation."""
    payload = {field: binding.get(field) for field in (
        "bridge_sha256", "adapter_sha256", "registry_hash", "rule_source_sha256",
        "frontend_version", "source_provenance",
    )}
    provenance = payload["source_provenance"]
    if isinstance(provenance, dict) and provenance.get("binding_scope") == "compiler_inputs_v1":
        # Source bytes are bound above by bridge/adapter/registry/rule hashes.
        # Git commit, branch and unrelated workspace changes remain audit data.
        payload["source_provenance"] = {field: provenance.get(field) for field in (
            "ready", "frontend_repo", "bridge_path", "binding_scope")}
    return payload


def design_content_hash(row: dict[str, Any]) -> str:
    return sha256_json(
        {
            "id": str(row.get("id") or row.get("design_id") or ""),
            "ref_id": str(row.get("ref_id") or ""),
            "data": str(row.get("data") or ""),
            "js_code": str(row.get("js_code") or ""),
            "csharp_code": str(row.get("csharp_code") or ""),
            "last_modification_time": str(row.get("last_modification_time") or ""),
        }
    )


RELATION_HASH_FIELDS = (
    "id",
    "isDeleted",
    "creatorId",
    "creationTime",
    "department",
    "lastModifierId",
    "lastModificationTime",
    "org_id",
    "org_name",
    "action_data_id",
    "design_id",
    "data",
)


def normalized_relation_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [
        {field: json.loads(json.dumps(row.get(field), default=json_default)) for field in RELATION_HASH_FIELDS}
        for row in rows
    ]
    return sorted(normalized, key=lambda item: (str(item.get("action_data_id") or ""), str(item.get("id") or "")))


def relation_snapshot_hash(rows: list[dict[str, Any]]) -> str:
    return sha256_json(normalized_relation_rows(rows))
