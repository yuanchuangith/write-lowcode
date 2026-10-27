from __future__ import annotations

import json
import hmac
import math
import re
import uuid
from datetime import datetime
from typing import Any

from .approval import statement_hash, transaction_binding_hash
from .action_context import action_context
from .hashing import compiler_binding_payload, design_content_hash, normalized_relation_rows, relation_snapshot_hash, sha256_json, sha256_text
from .single_row_checker import SingleRowCheckError, check_statement_list
from .logic_review import LogicReviewError, require_write_business_review, validate_business_review


class TransactionPlanError(RuntimeError):
    pass


def principal_verification(database_safety: dict[str, Any], current_principal: Any) -> str:
    """Compare against the original signed actor; never backfill old artifacts."""
    if "authenticated_principal" not in database_safety:
        return "legacy_grants_only"
    expected = database_safety["authenticated_principal"]
    for principal in (expected, current_principal):
        if (not isinstance(principal, str) or not principal or len(principal) > 512
                or any(ord(character) < 32 or ord(character) == 127 for character in principal)):
            raise TransactionPlanError("Database authenticated principal evidence is unavailable or invalid")
    if not hmac.compare_digest(expected.encode("utf-8"), current_principal.encode("utf-8")):
        raise TransactionPlanError("Database authenticated principal changed after approval planning")
    return "bound_principal_match"


UPDATE_DESIGN_SQL = (
    "UPDATE cpm_bizflows_design SET data=%s, js_code=%s, csharp_code=%s, "
    "lastModificationTime=%s WHERE id=%s AND is_publish=0 AND isDeleted=0 "
    "AND lastModificationTime=%s"
)
DELETE_RELATION_SQL = "DELETE FROM cpm_bizflows_design_relation_data WHERE id=%s AND design_Id=%s"
UPDATE_RELATION_SQL = (
    "UPDATE cpm_bizflows_design_relation_data SET data=%s, lastModifierId=%s, "
    "lastModificationTime=%s, isDeleted=0 WHERE id=%s AND design_Id=%s"
)
RESTORE_RELATION_SQL = (
    "UPDATE cpm_bizflows_design_relation_data SET isDeleted=%s, creatorId=%s, creationTime=%s, "
    "department=%s, lastModifierId=%s, lastModificationTime=%s, org_id=%s, org_name=%s, "
    "action_data_Id=%s, data=%s WHERE id=%s AND design_Id=%s"
)
INSERT_RELATION_SQL = (
    "INSERT INTO cpm_bizflows_design_relation_data "
    "(id,isDeleted,creatorId,creationTime,department,lastModifierId,lastModificationTime,"
    "org_id,org_name,action_data_Id,design_Id,data) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _statement(
    *,
    kind: str,
    sql: str,
    params: list[Any],
    expected_rows: int,
    summary: str,
    design_id: str,
    old_hash: str,
    new_hash: str,
) -> dict[str, Any]:
    value = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "sql": sql,
        "params": params,
        "expected_rows": expected_rows,
        "summary": summary,
        "design_id": design_id,
        "old_hash": old_hash,
        "new_hash": new_hash,
    }
    value["sql_sha256"] = sha256_text(sql)
    value["statement_hash"] = statement_hash(value)
    return value


def _desired_relation_rows(
    desired: dict[str, Any] | list[dict[str, Any]],
    baseline: dict[str, Any],
    current: list[dict[str, Any]],
    *,
    restore: bool,
) -> list[dict[str, Any]]:
    if restore:
        if not isinstance(desired, list):
            raise TransactionPlanError("Restore relations must be a list")
        return desired
    if not isinstance(desired, dict):
        raise TransactionPlanError("Compiler relation_data must be an object")
    current_by_action = {str(row.get("action_data_id") or ""): row for row in current}
    metadata_template = next((row for row in current if row.get("creatorId") and row.get("lastModifierId")), {})
    now = _now()
    rows: list[dict[str, Any]] = []
    for action_data_id, data in desired.items():
        existing = current_by_action.get(str(action_data_id))
        if existing:
            row = dict(existing)
            if existing.get("data") != data or existing.get("isDeleted") != 0:
                row["data"] = data
                row["isDeleted"] = 0
                row["lastModificationTime"] = now
        else:
            modifier = str(
                metadata_template.get("lastModifierId")
                or baseline.get("lastModifierId")
                or baseline.get("creatorId")
                or ""
            )
            creator = str(metadata_template.get("creatorId") or baseline.get("creatorId") or modifier)
            if not creator or not modifier:
                raise TransactionPlanError("Cannot create relation data without creator/modifier identity")
            row = {
                "id": uuid.uuid4().hex,
                "isDeleted": 0,
                "creatorId": creator,
                "creationTime": now,
                "department": metadata_template.get("department") or baseline.get("department"),
                "lastModifierId": modifier,
                "lastModificationTime": now,
                "org_id": metadata_template.get("org_id"),
                "org_name": metadata_template.get("org_name"),
                "action_data_id": str(action_data_id),
                "design_id": str(baseline["id"]),
                "data": data,
            }
        rows.append(row)
    return rows


def build_transaction_artifact(
    *,
    transaction_id: str,
    session_id: str,
    plan_id: str | None,
    kind: str,
    baseline: dict[str, Any],
    desired_design: dict[str, Any],
    current_relations: list[dict[str, Any]],
    desired_relations: dict[str, Any] | list[dict[str, Any]],
    database_safety: dict[str, Any],
    runtime_validation: dict[str, Any],
    compiler_evidence: dict[str, Any] | None = None,
    business_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    restore = kind == "restore"
    design_id = str(baseline["id"])
    old_hash = str(baseline["content_hash"])
    modified_time = str(desired_design.get("last_modification_time") or (_now() if not restore else ""))
    if not modified_time:
        raise TransactionPlanError("Desired last modification time is missing")
    desired_row = dict(baseline)
    desired_row.update(
        {
            "data": str(desired_design.get("data") or ""),
            "js_code": str(desired_design.get("js_code") or ""),
            "csharp_code": str(desired_design.get("csharp_code") or ""),
            "last_modification_time": modified_time,
        }
    )
    new_hash = design_content_hash(desired_row)
    statements = [
        _statement(
            kind="update_design",
            sql=UPDATE_DESIGN_SQL,
            params=[
                desired_row["data"],
                desired_row["js_code"],
                desired_row["csharp_code"],
                modified_time,
                design_id,
                str(baseline["last_modification_time"]),
            ],
            expected_rows=1,
            summary="Update exactly one unpublished draft row by primary key and optimistic timestamp",
            design_id=design_id,
            old_hash=old_hash,
            new_hash=new_hash,
        )
    ]

    wanted = _desired_relation_rows(desired_relations, baseline, current_relations, restore=restore)
    current_by_id = {str(row["id"]): row for row in current_relations}
    wanted_by_id = {str(row["id"]): row for row in wanted}
    for relation_id, row in current_by_id.items():
        if relation_id not in wanted_by_id:
            statements.append(
                _statement(
                    kind="delete_relation",
                    sql=DELETE_RELATION_SQL,
                    params=[relation_id, design_id],
                    expected_rows=1,
                    summary=f"Delete one explicit relation row for action node {row.get('action_data_id')}",
                    design_id=design_id,
                    old_hash=old_hash,
                    new_hash=new_hash,
                )
            )
    for relation_id, row in wanted_by_id.items():
        existing = current_by_id.get(relation_id)
        if existing:
            if (not restore and existing.get("data") == row.get("data")
                    and existing.get("isDeleted") == row.get("isDeleted")):
                continue
            restore_existing = restore
            statements.append(
                _statement(
                    kind="restore_relation" if restore_existing else "update_relation",
                    sql=RESTORE_RELATION_SQL if restore_existing else UPDATE_RELATION_SQL,
                    params=(
                        [
                            int(bool(row.get("isDeleted"))),
                            row.get("creatorId"),
                            row.get("creationTime"),
                            row.get("department"),
                            row.get("lastModifierId"),
                            row.get("lastModificationTime"),
                            row.get("org_id"),
                            row.get("org_name"),
                            row.get("action_data_id"),
                            row.get("data"),
                            relation_id,
                            design_id,
                        ]
                        if restore_existing
                        else [
                            row.get("data"),
                            row.get("lastModifierId"),
                            row.get("lastModificationTime"),
                            relation_id,
                            design_id,
                        ]
                    ),
                    expected_rows=1,
                    summary=f"Update one explicit relation row for action node {row.get('action_data_id')}",
                    design_id=design_id,
                    old_hash=old_hash,
                    new_hash=new_hash,
                )
            )
        else:
            statements.append(
                _statement(
                    kind="insert_relation",
                    sql=INSERT_RELATION_SQL,
                    params=[
                        relation_id,
                        int(bool(row.get("isDeleted"))),
                        row.get("creatorId"),
                        row.get("creationTime"),
                        row.get("department"),
                        row.get("lastModifierId"),
                        row.get("lastModificationTime"),
                        row.get("org_id"),
                        row.get("org_name"),
                        row.get("action_data_id"),
                        design_id,
                        row.get("data"),
                    ],
                    expected_rows=1,
                    summary=f"Insert one relation row for action node {row.get('action_data_id')}",
                    design_id=design_id,
                    old_hash=old_hash,
                    new_hash=new_hash,
                )
            )
    artifact = {
        "transaction_id": transaction_id,
        "session_id": session_id,
        "plan_id": plan_id,
        "kind": kind,
        "design_id": design_id,
        "ref_id": str(baseline["ref_id"]),
        "action_code": str(baseline.get("code") or ""),
        "action_context": action_context(baseline),
        "old_content_hash": old_hash,
        "old_relation_hash": relation_snapshot_hash(current_relations),
        "new_content_hash": new_hash,
        "baseline_last_modification_time": str(baseline["last_modification_time"]),
        "desired_last_modification_time": modified_time,
        "desired_design": {
            "data": desired_row["data"],
            "js_code": desired_row["js_code"],
            "csharp_code": desired_row["csharp_code"],
        },
        "expected_relation_rows": normalized_relation_rows(wanted),
        "expected_relation_hash": relation_snapshot_hash(wanted),
        "database_safety": {
            "account_mode": str(database_safety.get("account_mode") or ""),
            "grants_sha256": str(database_safety.get("grants_sha256") or ""),
            "shared_account": bool(database_safety.get("shared_account")),
            "privileged_shared_override": bool(database_safety.get("privileged_shared_override")),
        },
        "runtime_validation": runtime_validation,
        "compiler_evidence": compiler_evidence,
        "statements": statements,
        "approvals": {},
        "execution_started": False,
        "executed": False,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    # Optional for historic artifacts. New prepares freeze the actual actor
    # inside the existing signed container without changing its hash algorithm.
    if "authenticated_principal" in database_safety:
        artifact["database_safety"]["authenticated_principal"] = database_safety["authenticated_principal"]
    if "database_scope" in database_safety:
        artifact["database_safety"]["database_scope"] = database_safety["database_scope"]
    if business_review is not None:
        artifact["business_review"] = business_review
        if not restore:
            try:
                validate_business_review(business_review, baseline_design=baseline, candidate_design=desired_design,
                                         compiler_binding=(compiler_evidence or {}).get("compiler_binding"))
                require_write_business_review(artifact)
            except LogicReviewError as exc:
                raise TransactionPlanError(str(exc)) from exc
    artifact["transaction_hash"] = transaction_binding_hash(artifact)
    validate_transaction_artifact(artifact)
    return artifact


def _validate_event_compiler_evidence(desired: dict[str, Any], evidence: Any) -> None:
    """Check signed evidence structure; native recompilation is performed by the service."""
    if not isinstance(evidence, dict):
        raise TransactionPlanError("Event binding validation evidence is missing")
    try:
        canvas = json.loads(desired.get("data") or "")
    except (TypeError, ValueError) as exc:
        raise TransactionPlanError("Candidate canvas JSON is invalid") from exc
    if not isinstance(canvas, dict) or not isinstance(canvas.get("actionData"), list):
        raise TransactionPlanError("Candidate canvas actionData is invalid")
    groups: dict[str, dict[str, Any]] = {}
    nodes: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for group in canvas["actionData"]:
        if not isinstance(group, dict) or not isinstance(group.get("key"), str) or not group["key"] or group["key"] in groups:
            raise TransactionPlanError("Candidate canvas group identity is invalid")
        if not isinstance(group.get("data"), list):
            raise TransactionPlanError("Candidate canvas group nodes are invalid")
        groups[group["key"]] = group
        for node in group["data"]:
            if not isinstance(node, dict) or not isinstance(node.get("key"), str) or not node["key"]:
                raise TransactionPlanError("Candidate canvas node identity is invalid")
            nodes.setdefault(node["key"], []).append((group, node))
    js_keys = [key for key, group in groups.items() if group.get("actionType") == "js"]
    if evidence.get("javascript_syntax_checked") is not True:
        raise TransactionPlanError("Generated JavaScript syntax evidence is incomplete")
    if evidence.get("javascript_lexical_scope") != "all_js_groups" or evidence.get("javascript_group_keys") != js_keys:
        raise TransactionPlanError("Generated JavaScript lexical evidence does not cover the candidate groups")
    generation = evidence.get("generation")
    if generation is not None:
        if not isinstance(generation, dict) or generation.get("mode") != "incremental_action_groups" or generation.get("preserved_source_verified") is not True:
            raise TransactionPlanError("Incremental generation evidence is invalid")
        partition = []
        for field in ("regenerated_group_keys", "preserved_group_keys"):
            keys = generation.get(field)
            if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
                raise TransactionPlanError("Incremental generation group evidence is invalid")
            partition.extend(keys)
        if len(set(partition)) != len(partition) or set(partition) != set(groups):
            raise TransactionPlanError("Incremental generation evidence does not cover exactly the candidate groups")
        for field in ("baseline_sha256", "candidate_sha256"):
            if not re.fullmatch(r"[a-f0-9]{64}", str(generation.get(field) or "")):
                raise TransactionPlanError("Incremental generation baseline binding is invalid")
    diagnostics = evidence.get("baseline_diagnostics", [])
    if not isinstance(diagnostics, list):
        raise TransactionPlanError("Baseline diagnostics require matching source evidence")
    for finding in diagnostics:
        old = finding.get("baseline") if isinstance(finding, dict) else None
        if (not isinstance(finding, dict) or finding.get("group_key") not in js_keys
                or finding.get("reason_code") not in {"preexisting_undeclared_identifier",
                    "preexisting_reserved_platform_identifier", "preexisting_hidden_event_arguments"}
                or not isinstance(finding.get("identifier"), str) or not finding["identifier"]
                or finding.get("status") != "unverified" or finding.get("blocks_changed_candidate") is not False
                or not isinstance(old, dict)
                or any(not re.fullmatch(r"[a-f0-9]{64}", str(old.get(field) or ""))
                       for field in ("source_sha256", "reference_sha256"))
                or any(type(record.get("line")) is not int or record["line"] < 1
                       or type(record.get("column")) is not int or record["column"] < 0
                       for record in (finding, old))):
            raise TransactionPlanError("Baseline diagnostics must remain unverified and include matching source evidence")
    unreachable = evidence.get("unreachable_diagnostics", [])
    if not isinstance(unreachable, list):
        raise TransactionPlanError("Unreachable diagnostics must include static reachability evidence")
    for finding in unreachable:
        proof = finding.get("reachability") if isinstance(finding, dict) else None
        if (not isinstance(finding, dict) or finding.get("group_key") not in js_keys
                or finding.get("reason_code") not in {"undeclared_identifier", "hidden_event_arguments"}
                or not isinstance(finding.get("identifier"), str) or not finding["identifier"]
                or finding.get("status") != "unverified" or finding.get("blocks_changed_candidate") is not False
                or not isinstance(proof, dict) or not isinstance(proof.get("reason_code"), str) or proof["reason_code"] not in {
                    "after_return", "after_throw", "constant_true_alternate", "constant_false_consequent",
                    "constant_false_loop", "literal_short_circuit"}
                or any(type(record.get("line")) is not int or record["line"] < 1
                       or type(record.get("column")) is not int or record["column"] < 0
                       for record in (finding, proof))):
            raise TransactionPlanError("Unreachable diagnostics must remain unverified and include static reachability evidence")
    canvas_diagnostics = evidence.get("canvas_diagnostics", [])
    if not isinstance(canvas_diagnostics, list):
        raise TransactionPlanError("Canvas diagnostics require unchanged baseline evidence")
    for finding in canvas_diagnostics:
        if not isinstance(finding, dict):
            raise TransactionPlanError("Canvas diagnostics require unchanged baseline evidence")
        group_key, node_key, path = finding.get("group_key"), finding.get("node_key"), finding.get("path")
        snapshot = {"group_key": group_key, "node_key": node_key, "path": path}
        try:
            parts = path.split("/")[1:]
            if group_key is None and node_key is None and len(parts) == 2 and parts[0] == "globalVariables":
                value = canvas["globalVariables"][int(parts[1])]
                valid = finding.get("reason_code") == "preexisting_reserved_platform_identifier" and value.get("name") in {"inbiz", "Inbiz"}
                identifier = value.get("name")
            elif node_key is None and group_key in js_keys and len(parts) == 2 and parts[0] == "outputParams":
                value = groups[group_key]["outputParams"][int(parts[1])]
                valid = finding.get("reason_code") == "preexisting_reserved_platform_identifier" and value.get("title") in {"inbiz", "Inbiz"}
                identifier = value.get("title")
            else:
                owners = nodes.get(node_key, [])
                if len(owners) != 1 or owners[0][0]["key"] != group_key:
                    raise ValueError("Unknown node")
                node = owners[0][1]
                allowed = {"SetVariable": {("inputParams", "variableValue"), ("outputParams", "variableName")},
                           "SetVariableValue": {("inputParams", "attributeValue")}}
                value = node["paramsValue"][parts[0]][parts[1]]
                valid = (finding.get("reason_code") == "preexisting_paraminput_unsynchronized"
                         and tuple(parts) in allowed.get(node["elementKey"], set()) and value.get("paramTypes") == "custom"
                         and (not isinstance(value.get("value"), str) or value.get("value") != value.get("code")))
                snapshot["element_key"] = node["elementKey"]
                identifier = None
            snapshot["value"] = value
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise TransactionPlanError("Canvas diagnostic does not identify a candidate field") from exc
        digest = sha256_json(snapshot)
        if (not valid or any(finding.get(field) != value for field, value in snapshot.items())
                or (identifier is not None and finding.get("identifier") != identifier)
                or finding.get("status") != "unverified" or finding.get("blocks_changed_candidate") is not False
                or finding.get("baseline_snapshot_sha256") != digest or finding.get("candidate_snapshot_sha256") != digest):
            raise TransactionPlanError("Canvas diagnostics must match the unchanged field and remain unverified")
    scope, checks = evidence.get("binding_node_keys"), evidence.get("checks")
    if not isinstance(scope, list) or any(not isinstance(key, str) or not key for key in scope) or len(set(scope)) != len(scope):
        raise TransactionPlanError("Event binding validation scope is invalid")
    if not isinstance(checks, list) or len(checks) != len(scope):
        raise TransactionPlanError("Event binding validation checks are incomplete")
    for key, check in zip(scope, checks):
        owners = nodes.get(key, [])
        if len(owners) != 1 or owners[0][1].get("elementKey") != "ComponentEvent":
            raise TransactionPlanError("Event binding validation scope does not identify a candidate ComponentEvent")
        source, node = owners[0]
        params = node.get("paramsValue")
        inputs = params.get("inputParams") if isinstance(params, dict) else None
        name = inputs.get("name") if isinstance(inputs, dict) else None
        action = inputs.get("action") if isinstance(inputs, dict) else None
        if isinstance(check, dict) and "binding_issue" in check:
            issue = check["binding_issue"]
            target = groups.get(action.get("value")) if isinstance(action, dict) else None
            snapshot = {"binding_node_key": key, "source_group_key": source["key"],
                        "target_group_key": target["key"] if target else None,
                        "source_action_type": source.get("actionType"), "target_action_type": target.get("actionType") if target else None,
                        "binding_params": params, "input_params": (target.get("inputParams") or []) if target else [],
                        "output_params": (target.get("outputParams") or []) if target else []}
            digest = sha256_json(snapshot)
            if (not isinstance(issue, dict) or issue.get("reason_code") not in {"binding_component_identity_required",
                    "event_contract_unavailable", "event_callback_missing", "event_callback_group_invalid"}
                    or issue.get("status") != "unverified" or issue.get("blocks_changed_candidate") is not False
                    or issue.get("node_key") != key or issue.get("group_key") != source["key"]
                    or issue.get("baseline_signature_sha256") != digest or issue.get("candidate_signature_sha256") != digest
                    or check.get("source_group_key") != source["key"] or check.get("target_group_key") != snapshot["target_group_key"]
                    or check.get("runtime_verified") is not False):
                raise TransactionPlanError("Old event binding issue requires unchanged declarations and unverified evidence")
            continue
        if not isinstance(name, dict) or not isinstance(action, dict) or not isinstance(action.get("value"), str):
            raise TransactionPlanError("Event binding candidate parameters are invalid")
        target = groups.get(action["value"])
        if (not isinstance(check, dict) or target is None or source.get("actionType") != "js"
                or target.get("actionType") != "js" or source is target):
            raise TransactionPlanError("Event binding validation target is invalid")
        component = name.get("value")
        if name.get("paramTypes") != "componentsVariable" or not isinstance(component, str) or not component.startswith("componentsVariable-"):
            raise TransactionPlanError("Event binding component declaration is invalid")
        expected = {"binding_node_key": key, "source_group_key": source["key"], "target_group_key": target["key"],
                    "component_id": component[len("componentsVariable-"):], "component_type": name.get("dataType"),
                    "event_name": inputs.get("eventName"), "input_params": target.get("inputParams") or [],
                    "output_params": target.get("outputParams") or []}
        if not isinstance(expected["input_params"], list) or not isinstance(expected["output_params"], list):
            raise TransactionPlanError("Event callback parameter declarations are invalid")
        if any(check.get(field) != value for field, value in expected.items()):
            raise TransactionPlanError("Event binding validation check does not match the candidate canvas")
        if (not all(isinstance(expected[field], str) and expected[field] for field in ("component_id", "component_type", "event_name"))
                or check.get("parameters_declared") is not True or check.get("hidden_arguments_allowed") is not False
                or check.get("signature_status") not in {"source_verified", "catalog_declared"}):
            raise TransactionPlanError("Event binding runtime contract evidence is incomplete")
        parameter_issues = check.get("parameter_issues", [])
        if not isinstance(parameter_issues, list) or (parameter_issues and check.get("parameter_status") != "preexisting_mismatch"):
            raise TransactionPlanError("Event binding parameter mismatch evidence is incomplete")
        snapshot = {"binding_node_key": key, "source_group_key": source["key"], "target_group_key": target["key"],
                    "source_action_type": source.get("actionType"), "target_action_type": target.get("actionType"),
                    "binding_params": params, "input_params": target.get("inputParams") or [],
                    "output_params": target.get("outputParams") or []}
        signature_sha256 = sha256_json(snapshot)
        sections = set()
        for issue in parameter_issues:
            section = issue.get("section") if isinstance(issue, dict) else None
            if (not isinstance(issue, dict) or not isinstance(section, str) or section not in {"inputParams", "outputParams"}
                    or section in sections or issue.get("reason_code") != "preexisting_callback_parameter_mismatch"
                    or issue.get("status") != "unverified" or issue.get("blocks_changed_candidate") is not False
                    or issue.get("baseline_signature_sha256") != signature_sha256
                    or issue.get("candidate_signature_sha256") != signature_sha256
                    or issue.get("actual_params") != (target.get(section) or [])
                    or not isinstance(issue.get("expected_params"), list)):
                raise TransactionPlanError("Event binding parameter mismatch requires unchanged baseline evidence and unverified status")
            sections.add(section)
        if check.get("parameter_status") == "preexisting_mismatch" and not parameter_issues:
            raise TransactionPlanError("Event binding parameter mismatch evidence is incomplete")
    if "preexisting_parameter_issue_count" in evidence and (
            type(evidence["preexisting_parameter_issue_count"]) is not int
            or evidence["preexisting_parameter_issue_count"] != sum(len(check.get("parameter_issues", [])) for check in checks)):
        raise TransactionPlanError("Event binding parameter mismatch count is invalid")


def _validate_generation_selection(desired: dict[str, Any], compiler_evidence: dict[str, Any]) -> None:
    """Validate signed strategy evidence; the service independently recompiles it."""
    event = compiler_evidence["event_binding_validation"]
    if "generation_selection" not in compiler_evidence and "generation_selection" not in event:
        return  # Existing artifacts created before selectable generation remain valid.
    selection = compiler_evidence.get("generation_selection")
    if not isinstance(selection, dict) or selection != event.get("generation_selection"):
        raise TransactionPlanError("Generation selection evidence changed or is incomplete")
    requested, selected = selection.get("requested_mode"), selection.get("selected_mode")
    if not isinstance(requested, str) or requested not in {"full", "incremental", "auto"} or not isinstance(selected, str) or selected not in {"full", "incremental"}:
        raise TransactionPlanError("Generation selection mode is invalid")
    if selection.get("policy_version") != "action_groups_v1":
        raise TransactionPlanError("Generation selection policy is invalid")
    for field in ("baseline_sha256", "candidate_sha256"):
        if not isinstance(selection.get(field), str) or not re.fullmatch(r"[a-f0-9]{64}", selection[field]):
            raise TransactionPlanError("Generation selection hash binding is invalid")
    thresholds = {"max_affected_groups": 3, "max_changed_nodes": 30, "max_affected_group_ratio": 0.5}
    if selection.get("thresholds") != thresholds:
        raise TransactionPlanError("Generation selection thresholds do not match the policy")
    counts, scope = selection.get("counts"), selection.get("scope")
    integer_counts = ("total_group_count", "direct_changed_group_count", "affected_group_count",
                      "dependency_group_count", "changed_node_count", "language_changed_group_count")
    if not isinstance(counts, dict) or any(type(counts.get(field)) is not int or counts[field] < 0 for field in integer_counts):
        raise TransactionPlanError("Generation selection counts are invalid")
    ratio = counts.get("affected_group_ratio")
    if type(ratio) not in (int, float) or not 0 <= ratio <= 1 or not math.isfinite(ratio):
        raise TransactionPlanError("Generation selection affected group ratio is invalid")
    if not isinstance(scope, dict) or type(scope.get("shared_context_changed")) is not bool:
        raise TransactionPlanError("Generation selection scope is invalid")
    for field in ("affected_group_keys", "unaffected_group_keys", "removed_group_keys"):
        keys = scope.get(field)
        if not isinstance(keys, list) or any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != len(keys):
            raise TransactionPlanError("Generation selection group scope is invalid")
    canvas = json.loads(desired["data"])
    candidate_keys = {group["key"] for group in canvas["actionData"]}
    affected = scope["affected_group_keys"]
    unaffected = scope["unaffected_group_keys"]
    removed = scope["removed_group_keys"]
    if set(affected) & set(unaffected) or set(affected + unaffected) != candidate_keys or set(removed) & candidate_keys:
        raise TransactionPlanError("Generation selection scope does not cover exactly the candidate groups")
    if scope["shared_context_changed"] and unaffected:
        raise TransactionPlanError("Generation selection shared context change must affect all candidate groups")
    total = len(candidate_keys) + len(removed)
    affected_count = len(affected) + len(removed)
    direct_count = counts["direct_changed_group_count"]
    if (counts["total_group_count"] != total or counts["affected_group_count"] != affected_count
            or direct_count > affected_count or counts["dependency_group_count"] != affected_count - direct_count
            or counts["language_changed_group_count"] > direct_count
            or ratio != (affected_count / total if total else 0)):
        raise TransactionPlanError("Generation selection counts do not match its group scope")
    generation = compiler_evidence.get("generation")
    if selected == "full":
        if generation is not None:
            raise TransactionPlanError("Full generation selection cannot claim incremental preservation")
    else:
        if not isinstance(generation, dict):
            raise TransactionPlanError("Incremental generation selection requires preservation evidence")
        expected = {"regenerated_group_keys": affected, "preserved_group_keys": unaffected,
                    "removed_group_keys": removed, "shared_context_changed": scope["shared_context_changed"],
                    "baseline_sha256": selection["baseline_sha256"], "candidate_sha256": selection["candidate_sha256"]}
        if any(generation.get(field) != value for field, value in expected.items()):
            raise TransactionPlanError("Generation selection and incremental preservation evidence disagree")
    reason = selection.get("reason_code")
    if not isinstance(reason, str):
        raise TransactionPlanError("Generation selection reason is invalid")
    if requested != "auto":
        if selected != requested or reason != "explicit_" + requested:
            raise TransactionPlanError("Generation selection does not honor the explicit requested mode")
    elif reason in {"baseline_unavailable", "baseline_artifacts_unavailable"}:
        if selected != "full":
            raise TransactionPlanError("Generation selection requires full generation without a saved baseline")
    else:
        if scope["shared_context_changed"]:
            expected_reason = "shared_context_changed"
        elif counts["language_changed_group_count"]:
            expected_reason = "language_changed"
        elif affected_count > thresholds["max_affected_groups"]:
            expected_reason = "affected_group_limit"
        elif counts["changed_node_count"] > thresholds["max_changed_nodes"]:
            expected_reason = "changed_node_limit"
        elif ratio >= thresholds["max_affected_group_ratio"]:
            expected_reason = "affected_group_ratio"
        else:
            expected_reason = "small_change"
        if reason != expected_reason or selected != ("incremental" if expected_reason == "small_change" else "full"):
            raise TransactionPlanError("Generation selection does not match the automatic policy")


def validate_transaction_artifact(artifact: dict[str, Any]) -> None:
    transaction_id = str(artifact.get("transaction_id") or "")
    design_id = str(artifact.get("design_id") or "")
    if not transaction_id or not design_id:
        raise TransactionPlanError("Transaction artifact is missing its identity")
    if not isinstance(artifact.get("execution_started", False), bool) or not isinstance(artifact.get("executed", False), bool):
        raise TransactionPlanError("Transaction execution state is invalid")
    if artifact.get("transaction_hash") != transaction_binding_hash(artifact):
        raise TransactionPlanError("Transaction metadata hash is invalid")
    context = artifact.get("action_context")
    if "action_context" in artifact and (not isinstance(context, dict) or context.get("schema_version") != 1
        or context.get("design_id") != str(design_id) or context.get("ref_id") != str(artifact.get("ref_id") or "")):
        raise TransactionPlanError("Transaction form/action context does not match its target")
    database_safety = artifact.get("database_safety")
    if not isinstance(database_safety, dict):
        raise TransactionPlanError("Transaction database safety binding is missing")
    account_mode = str(database_safety.get("account_mode") or "")
    if account_mode not in {"dedicated", "shared_restricted", "shared_unrestricted"}:
        raise TransactionPlanError("Transaction database account mode is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(database_safety.get("grants_sha256") or "")):
        raise TransactionPlanError("Transaction database grants fingerprint is invalid")
    if bool(database_safety.get("shared_account")) != (account_mode != "dedicated"):
        raise TransactionPlanError("Transaction shared account binding is inconsistent")
    if bool(database_safety.get("privileged_shared_override")) != (account_mode == "shared_unrestricted"):
        raise TransactionPlanError("Transaction privileged shared-account binding is inconsistent")
    if "authenticated_principal" in database_safety:
        # Structural validation only; runtime comparison uses a freshly read
        # authenticated actor rather than this value compared with itself.
        principal = database_safety["authenticated_principal"]
        if (not isinstance(principal, str) or not principal or len(principal) > 512
                or any(ord(character) < 32 or ord(character) == 127 for character in principal)):
            raise TransactionPlanError("Transaction authenticated principal binding is invalid")
    if "database_scope" in database_safety and not re.fullmatch(r"[0-9a-f]{64}", str(database_safety["database_scope"])):
        raise TransactionPlanError("Transaction database scope binding is invalid")
    statements = artifact.get("statements")
    if not isinstance(statements, list) or not statements:
        raise TransactionPlanError("Transaction artifact has no statements")
    try:
        check_statement_list(statements)
    except SingleRowCheckError as exc:
        raise TransactionPlanError(str(exc)) from exc
    expected_sql = {
        "update_design": (UPDATE_DESIGN_SQL, 6),
        "delete_relation": (DELETE_RELATION_SQL, 2),
        "update_relation": (UPDATE_RELATION_SQL, 5),
        "restore_relation": (RESTORE_RELATION_SQL, 12),
        "insert_relation": (INSERT_RELATION_SQL, 12),
    }
    seen_ids: set[str] = set()
    for statement in statements:
        statement_id = str(statement.get("id") or "")
        if not statement_id or statement_id in seen_ids:
            raise TransactionPlanError("Transaction contains a missing or duplicate statement id")
        seen_ids.add(statement_id)
        kind = str(statement.get("kind") or "")
        if kind not in expected_sql:
            raise TransactionPlanError(f"Unsupported transaction statement kind: {kind}")
        sql, param_count = expected_sql[kind]
        if statement.get("sql") != sql or ";" in str(statement.get("sql") or ""):
            raise TransactionPlanError(f"Statement {statement_id} does not match its approved SQL template")
        params = statement.get("params")
        if not isinstance(params, list) or len(params) != param_count:
            raise TransactionPlanError(f"Statement {statement_id} has an invalid parameter count")
        if int(statement.get("expected_rows") or 0) != 1:
            raise TransactionPlanError(f"Statement {statement_id} must affect exactly one row")
        if str(statement.get("design_id") or "") != design_id:
            raise TransactionPlanError(f"Statement {statement_id} targets another design")
        if statement.get("old_hash") != artifact.get("old_content_hash") or statement.get("new_hash") != artifact.get("new_content_hash"):
            raise TransactionPlanError(f"Statement {statement_id} hash binding is invalid")
        if statement.get("sql_sha256") != sha256_text(sql) or statement.get("statement_hash") != statement_hash(statement):
            raise TransactionPlanError(f"Statement {statement_id} content hash is invalid")
        if kind == "update_design" and (str(params[4]) != design_id or str(params[5]) != str(artifact.get("baseline_last_modification_time"))):
            raise TransactionPlanError("Draft update optimistic-lock parameters are invalid")
        if kind in {"delete_relation", "update_relation", "restore_relation"} and str(params[-1]) != design_id:
            raise TransactionPlanError(f"Statement {statement_id} relation ownership guard is invalid")
        if kind == "insert_relation" and str(params[10]) != design_id:
            raise TransactionPlanError(f"Statement {statement_id} relation ownership is invalid")
    relation_rows = artifact.get("expected_relation_rows")
    if not isinstance(relation_rows, list) or relation_snapshot_hash(relation_rows) != artifact.get("expected_relation_hash"):
        raise TransactionPlanError("Expected relation snapshot hash is invalid")
    runtime_validation = artifact.get("runtime_validation")
    if not isinstance(runtime_validation, dict):
        raise TransactionPlanError("Runtime validation evidence is missing")
    for field in ("available", "success", "production_compatible", "zero_backend_mode"):
        if not isinstance(runtime_validation.get(field), bool):
            raise TransactionPlanError(f"Runtime validation field {field} is invalid")
    if runtime_validation.get("source_mode") not in {"development_build", "deployment_snapshot"}:
        raise TransactionPlanError("Runtime validation mode is invalid")
    if not runtime_validation.get("zero_backend_mode"):
        raise TransactionPlanError("Runtime validation must remain local and read-only")
    reference_hash = str(runtime_validation.get("reference_manifest_sha256") or "")
    if reference_hash and (
        len(reference_hash) != 64
        or any(ch not in "0123456789abcdef" for ch in reference_hash.lower())
    ):
        raise TransactionPlanError("Runtime reference manifest hash is invalid")
    if runtime_validation.get("csharp_sha256") != sha256_text(
        str((artifact.get("desired_design") or {}).get("csharp_code") or "")
    ):
        raise TransactionPlanError("Runtime C# validation hash does not match the desired design")
    if artifact.get("kind") == "write" and str((artifact.get("desired_design") or {}).get("csharp_code") or "").strip():
        if runtime_validation.get("available") is not True or runtime_validation.get("success") is not True:
            raise TransactionPlanError("Candidate C# compilation must succeed before a write approval")
    compiler_evidence = artifact.get("compiler_evidence")
    if artifact.get("kind") == "write":
        if not isinstance(compiler_evidence, dict):
            raise TransactionPlanError("Blocking compiler evidence is missing")
        binding = compiler_evidence.get("compiler_binding")
        validation = compiler_evidence.get("validation")
        hashes = compiler_evidence.get("hashes")
        if not isinstance(binding, dict) or not isinstance(validation, dict) or not isinstance(hashes, dict):
            raise TransactionPlanError("Blocking compiler evidence is incomplete")
        binding_payload = compiler_binding_payload(binding)
        for field in ("bridge_sha256", "adapter_sha256", "registry_hash", "rule_source_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(binding.get(field) or "").lower()):
                raise TransactionPlanError(f"Compiler binding field {field} is invalid")
        if binding.get("compiler_binding_sha256") != sha256_json(binding_payload):
            raise TransactionPlanError("Compiler binding hash is invalid")
        provenance = binding.get("source_provenance")
        if not isinstance(provenance, dict) or provenance.get("ready") is not True:
            raise TransactionPlanError("Frontend source provenance is not verified")
        if not re.fullmatch(r"[0-9a-f]{40,64}", str(provenance.get("commit") or "").lower()):
            raise TransactionPlanError("Frontend source commit is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("workspace_sha256") or "").lower()):
            raise TransactionPlanError("Frontend source workspace hash is invalid")
        if validation.get("valid") is not True or compiler_evidence.get("code_generation_complete") is not True:
            raise TransactionPlanError("Candidate canvas or generated-code validation is not complete")
        if validation.get("registry_hash") != binding.get("registry_hash"):
            raise TransactionPlanError("Candidate validation registry hash does not match the compiler binding")
        if validation.get("rule_source_sha256") != binding.get("rule_source_sha256"):
            raise TransactionPlanError("Candidate validation rule hash does not match the compiler binding")
        desired = artifact.get("desired_design") or {}
        if compiler_evidence.get("generated_code_consistency_verified") is not True:
            raise TransactionPlanError("Native generated-code consistency evidence is missing")
        _validate_event_compiler_evidence(desired, compiler_evidence.get("event_binding_validation"))
        if compiler_evidence.get("generation") != compiler_evidence["event_binding_validation"].get("generation"):
            raise TransactionPlanError("Incremental generation evidence changed")
        _validate_generation_selection(desired, compiler_evidence)
        expected_hashes = {
            "data_sha256": sha256_text(str(desired.get("data") or "")),
            "js_sha256": sha256_text(str(desired.get("js_code") or "")),
            "csharp_sha256": sha256_text(str(desired.get("csharp_code") or "")),
        }
        if any(hashes.get(field) != value for field, value in expected_hashes.items()):
            raise TransactionPlanError("Compiler evidence does not match the desired generated code")
        if not re.fullmatch(r"[0-9a-f]{64}", str(hashes.get("relation_sha256") or "").lower()):
            raise TransactionPlanError("Compiler relation hash is invalid")
    elif compiler_evidence is not None:
        raise TransactionPlanError("Restore transactions must not claim new compiler evidence")
    if "business_review" in artifact:
        try:
            validate_business_review(artifact["business_review"], candidate_design=artifact.get("desired_design"),
                                     candidate_hashes=(artifact.get("compiler_evidence") or {}).get("hashes"),
                                     compiler_binding=(artifact.get("compiler_evidence") or {}).get("compiler_binding"))
        except LogicReviewError as exc:
            raise TransactionPlanError(str(exc)) from exc
