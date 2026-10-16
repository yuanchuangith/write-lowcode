from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MCP_ROOT = PLUGIN_ROOT / "mcp"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

from gxp_editor.config import DatabaseConfig, EditorConfig, RuntimeReferenceConfig
from gxp_editor.hashing import design_content_hash, sha256_json, sha256_text
from gxp_editor.transaction import build_transaction_artifact


def editor_config() -> EditorConfig:
    return EditorConfig(
        database=DatabaseConfig(host="127.0.0.1", port=3306, database="test", user="editor"),
        frontend_repo=str(PLUGIN_ROOT),
        compiler_bridge="bridge.js",
        runtime_references=RuntimeReferenceConfig(
            directory=str(PLUGIN_ROOT),
            source_mode="deployment_snapshot",
        ),
        approval_ttl_minutes=15,
    )


def baseline(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "design-1",
        "ref_id": "ref-1",
        "code": "ActionOne",
        "name": "Action one",
        "data": '{"actionData":[],"globalVariables":[]}',
        "js_code": "old-js",
        "csharp_code": "old-csharp",
        "last_modification_time": "2026-07-26 08:00:00.123",
        "is_publish": 0,
        "isDeleted": 0,
        "creatorId": "creator-1",
        "lastModifierId": "modifier-1",
        "department": "dept-1",
        "metadata": {"action_code": "ActionOne", "action_name": "Action one"},
    }
    row.update(overrides)
    row["content_hash"] = design_content_hash(row)
    return row


def relation(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "relation-1",
        "isDeleted": 0,
        "creatorId": "creator-1",
        "creationTime": "2026-07-20 08:00:00.000",
        "department": "dept-1",
        "lastModifierId": "modifier-1",
        "lastModificationTime": "2026-07-25 08:00:00.000",
        "org_id": "org-1",
        "org_name": "Org one",
        "action_data_id": "node-1",
        "design_id": "design-1",
        "data": '{"old":true}',
    }
    row.update(overrides)
    return row


def runtime_validation(csharp_code: str) -> dict[str, Any]:
    return {
        "available": True,
        "success": True,
        "production_compatible": True,
        "source_mode": "deployment_snapshot",
        "zero_backend_mode": True,
        "target_framework": "net6.0",
        "reference_count": 4,
        "reference_manifest_sha256": "a" * 64,
        "csharp_sha256": sha256_text(csharp_code),
    }


def transaction(*, kind: str = "write", current_relations=None, desired_relations=None, database_safety=None):
    base = baseline()
    current_relations = list(current_relations or [])
    desired_relations = {} if desired_relations is None else desired_relations
    desired = {
        "data": '{"actionData":[],"globalVariables":[]}',
        "js_code": "new-js",
        "csharp_code": "new-csharp",
    }
    if kind == "restore":
        desired["last_modification_time"] = "2026-07-20 07:00:00.111"
    if database_safety is None:
        database_safety = {
            "account_mode": "dedicated",
            "grants_sha256": "c" * 64,
            "shared_account": False,
            "privileged_shared_override": False,
        }
    provenance = {
        "ready": True,
        "frontend_repo": str(PLUGIN_ROOT),
        "commit": "d" * 40,
        "branch": "test",
        "dirty": False,
        "dirty_file_count": 0,
        "workspace_sha256": "e" * 64,
        "bridge_path": "bridge.js",
    }
    binding_payload = {
        "bridge_sha256": "a" * 64,
        "adapter_sha256": "f" * 64,
        "registry_hash": "b" * 64,
        "rule_source_sha256": "c" * 64,
        "frontend_version": "test",
        "source_provenance": provenance,
    }
    compiler_binding = {
        **binding_payload,
        "compiler_binding_sha256": sha256_json(binding_payload),
    }
    compiler_evidence = None
    if kind == "write":
        compiler_evidence = {
            "compiler_binding": compiler_binding,
            "validation": {
                "valid": True,
                "action_count": 0,
                "node_count": 0,
                "registry_hash": binding_payload["registry_hash"],
                "rule_source_sha256": binding_payload["rule_source_sha256"],
                "frontend_version": binding_payload["frontend_version"],
            },
            "code_generation_complete": True,
            "generated_code_consistency_verified": True,
            "event_binding_validation": {"binding_node_keys": [], "checks": [],
                "javascript_syntax_checked": True, "javascript_lexical_scope": "all_js_groups", "javascript_group_keys": []},
            "hashes": {
                "data_sha256": sha256_text(desired["data"]),
                "js_sha256": sha256_text(desired["js_code"]),
                "csharp_sha256": sha256_text(desired["csharp_code"]),
                "relation_sha256": sha256_json(desired_relations),
            },
        }
    return build_transaction_artifact(
        transaction_id="transaction-1",
        session_id="session-1",
        plan_id="plan-1" if kind == "write" else None,
        kind=kind,
        baseline=base,
        desired_design=desired,
        current_relations=current_relations,
        desired_relations=desired_relations,
        database_safety=database_safety,
        runtime_validation=runtime_validation(desired["csharp_code"]),
        compiler_evidence=compiler_evidence,
    )
