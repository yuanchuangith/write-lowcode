"""Cheap, data-only operation checks. This does not certify native semantics."""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .canvas_assertions import CanvasAssertionError, validate_json_value
from .planning_limits import MAX_OPERATION_BYTES


CONTRACT_PATH = Path(__file__).with_name("operation_contracts.json")
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
SCHEMA_VERSION = CONTRACT["schema_version"]
MAX_ISSUES = 20
UNSAFE = set(CONTRACT["unsafe_keys"])
_PARAMETER_OPS = {"set_parameter", "set_array_item"}
_ALIAS_CREATORS = {"append_node", "insert_in_block", "insert_before", "insert_after"}
_ALIAS_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TYPES = {"string", "number", "double", "boolean", "object", "dictionary", "array", "dateTime", "any"}


def operation_shape(name: str) -> dict[str, Any]:
    """Descriptions only: no parameter values or executable business code."""
    rule = CONTRACT["operations"].get(name) if type(name) is str else None
    if not rule:
        return {"supported_operations": list(CONTRACT["operations"])}
    return {"operation": name, "required_fields": rule["required"][:],
            "optional_fields": rule["optional"][:], "strict_fields": rule["strict_fields"]}


def atomic_parameters(element: str) -> list[list[str]]:
    return deepcopy(CONTRACT["atomic_parameters"].get(element, []))


def diagnostic_context_ref(design: Any, issue: Any) -> dict[str, Any]:
    """Return a read hint only for proven canvas identities, never parameter data.

    Group-only JS source locations are not node mappings. Node aliases/new
    generated nodes cannot be inspected in the saved session baseline.
    """
    if type(issue) is not dict:
        return {"scope": "submitted_operations", "available": False}
    group_key, node_key = issue.get("group_key"), issue.get("node_key")
    groups = design.get("actionData") if type(design) is dict else None
    matches = [group for group in groups if type(group) is dict and group.get("key") == group_key] if type(groups) is list and _text(group_key) else []
    if len(matches) != 1:
        return {"scope": "group_only" if issue.get("mapping_status") == "group_only" else "submitted_operations",
                "available": False, "reason_code": "baseline_target_unresolved",
                **({"group_key": group_key[:256]} if _text(group_key) else {})}
    group = matches[0]
    nodes = group.get("data")
    exact = [node for node in nodes if type(node) is dict and node.get("key") == node_key] if type(nodes) is list and _text(node_key) else []
    if len(exact) == 1 and issue.get("mapping_status") != "group_only":
        return {"scope": "node", "tool": "inspect_edit_context",
                "arguments": {"targets": [{"group_key": group_key, "node_key": node_key}], "operation_cursor": 0},
                "arguments_from_failed_request": ["session_id", "conversation_id"],
                "baseline_target_verified": True}
    # Actual readonly inspect_action accepts an exact group key, bounded node
    # paging, and value_path/value_offset continuation. No node is invented.
    arguments = {"version": "draft", "group": group_key, "include_params": False,
                 "max_nodes": 20, "max_output_bytes": 32768}
    identifier = next((design.get(key) for key in ("refId", "code", "id") if _text(design.get(key))), None)
    if identifier is not None:
        arguments["identifier"] = identifier[:256]
    if _text(design.get("id")):
        arguments["design_id"] = design["id"][:256]
    return {"scope": "group_only", "tool": "inspect_action", "provider": "gxp-lowcode-readonly",
            "arguments": arguments, "baseline_target_verified": True,
            **({"required_arguments": ["identifier"]} if identifier is None else {})}


def enrich_diagnostics(design: Any, details: Any) -> dict[str, Any]:
    """Bound error continuation without a new endpoint, persistence or codegen."""
    if type(details) is not dict:
        return {}
    result = dict(details)
    result["context_ref"] = diagnostic_context_ref(design, details)
    if type(details.get("issues")) is list:
        result["issues"] = [{**row, "context_ref": diagnostic_context_ref(design, row)}
                            for row in details["issues"][:MAX_ISSUES] if type(row) is dict]
        truncated = bool(details.get("issues_truncated")) or len(details["issues"]) > len(result["issues"])
        result["issues_truncated"] = truncated
        result["next_step"] = ((f"Only the first {MAX_ISSUES} issues are returned. " if truncated else "")
            + "Use each context_ref for an optional focused read; parameter values are not copied into errors. "
              "Correct the returned issues, then resubmit the complete batch against the same session baseline and business review expectations. "
            + ("Remaining issues are returned after the first issues are corrected; there is no stored error page or diagnostics cursor." if truncated else ""))
    else:
        result["next_step"] = ("Use context_ref for an optional focused read; parameter values are not copied into errors. "
                               "Correct the reported issue and resubmit the complete batch against the same session baseline and business review expectations.")
    return result


def same_json(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return type(left) in (int, float) and type(right) in (int, float) and left == right
    if type(left) is dict:
        return left.keys() == right.keys() and all(same_json(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return left == right


def pointer_tokens(path: Any) -> list[str]:
    if type(path) is not str or not path.strip() or not path.startswith("/") or re.search(r"~(?![01])", path):
        raise _Invalid("invalid_parameter_path", "path must be a valid JSON pointer")
    parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
    if len(parts) < 2 or parts[0] not in {"inputParams", "outputParams"} or UNSAFE.intersection(parts):
        raise _Invalid("invalid_parameter_path", "path must select an existing safe inputParams/outputParams parameter")
    return parts


class _Invalid(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _text(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _safe_parameter(value: Any) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if type(current) is dict:
            if UNSAFE.intersection(current):
                raise _Invalid("invalid_parameter_value", "Parameter value contains an unsafe object key")
            pending.extend(current.values())
        elif type(current) is list:
            pending.extend(current)


def _record_keys(before: Any, after: Any) -> None:
    if type(before) is list:
        if type(after) is not list:
            raise _Invalid("invalid_parameter_value", "An array parameter must remain an array")
    elif type(before) is dict:
        if type(after) is not dict or before.keys() != after.keys():
            raise _Invalid("invalid_parameter_value", "Parameter record replacement must preserve existing keys")
        for key in before:
            _record_keys(before[key], after[key])
    elif type(after) in (dict, list):
        raise _Invalid("invalid_parameter_value", "A scalar parameter cannot become a container")


def _item_selector(value: Any) -> tuple[str, Any]:
    if type(value) is not dict or set(value) != {"field", "value"}:
        raise _Invalid("invalid_item_key", "item_key must contain only field and value")
    key, item = value["field"], value["value"]
    if key not in ("id", "key") or not (_text(item) or type(item) in (int, float) and math.isfinite(item) and item == int(item) and abs(item) <= 9007199254740991):
        raise _Invalid("invalid_item_key", "item_key must select a native id/key by nonblank string or safe integer")
    return key, item


def _declarations(value: Any) -> None:
    if type(value) is not list:
        raise _Invalid("invalid_operation", "Parameter declarations must be an array")
    seen = set()
    for row in value:
        if (type(row) is not dict or set(row) - {"title", "type", "description", "required"}
                or not _text(row.get("title")) or not _ALIAS_RE.fullmatch(row["title"])
                or row["title"] in seen or type(row.get("type")) is not str or row["type"] not in _TYPES
                or "required" in row and type(row["required"]) is not bool
                or "description" in row and type(row["description"]) is not str):
            raise _Invalid("invalid_operation", "Parameter declarations require unique native names and supported types")
        seen.add(row["title"])


def _parameter_update(node: dict, op: dict) -> bool:
    if node.get("elementKey") != op["expected_element_key"]:
        raise _Invalid("element_type_mismatch", "Target element type changed; inspect the current node")
    parts = pointer_tokens(op["path"])
    params = node.get("paramsValue")
    if type(params) is not dict or any(name in params and type(params[name]) is not dict for name in ("inputParams", "outputParams")):
        raise _Invalid("invalid_parameter_value", "Target parameters require input/output dictionaries")
    container = params
    for part in parts[:-1]:
        if type(container) is list:
            raise _Invalid("array_index_forbidden", "Use a native array row key or replace the whole guarded array")
        if type(container) is not dict or part not in container:
            raise _Invalid("parameter_path_missing", "Parameter path does not exist")
        container = container[part]
    if type(container) is list:
        raise _Invalid("array_index_forbidden", "Use a native array row key or replace the whole guarded array")
    selected = parts[-1]
    if type(container) is not dict or selected not in container:
        raise _Invalid("parameter_path_missing", "Parameter path does not exist")
    if op["operation"] == "set_array_item":
        rows = container[selected]
        if type(rows) is not list:
            raise _Invalid("array_parameter_required", "set_array_item path must select an existing array")
        field, value = _item_selector(op["item_key"])
        matches = [index for index, row in enumerate(rows) if type(row) is dict and field in row and same_json(row[field], value)]
        if len(matches) != 1:
            raise _Invalid("ambiguous_item_key" if matches else "item_key_not_found", "Native row key must select exactly one item")
        container, selected = rows, matches[0]
        if type(op["value"]) is not dict or any(key in container[selected] and not same_json(container[selected][key], op["value"].get(key)) for key in ("id", "key")):
            raise _Invalid("item_identity_changed", "Replacement must preserve native row id/key")
    _safe_parameter(op["expected_old_value"])
    _safe_parameter(op["value"])
    if not same_json(container[selected], op["expected_old_value"]):
        raise _Invalid("old_value_mismatch", "Parameter old value changed; inspect the current node")
    _record_keys(container[selected], op["value"])
    for section, name in atomic_parameters(node.get("elementKey")):
        if parts[:2] == [section, name] and len(parts) != 2:
            raise _Invalid("paraminput_record_required", "Submit the complete ParamInput record so value/code stay synchronized")
    unchanged = same_json(container[selected], op["value"])
    container[selected] = deepcopy(op["value"])
    for section, name in atomic_parameters(node.get("elementKey")):
        record = params.get(section, {}).get(name)
        if type(record) is dict and record.get("paramTypes") == "custom" and (type(record.get("value")) is not str or record.get("value") != record.get("code")):
            raise _Invalid("paraminput_unsynchronized", "Custom ParamInput value and code must stay synchronized")
    return unchanged


def preflight_batch(design: Any, operations: Any) -> dict[str, Any]:
    """Reject only proven errors; unknown native/alias outcomes remain pending.

    Sequential parameter changes commit only after the complete operation
    validates. A rejected operation cannot poison independent later checks.
    A native
    structural operation makes its group opaque for later target checks; no
    guessed generated keys, block layout or parameter defaults are introduced.
    """
    issues, pending, issue_count = [], [], 0
    report = {"schema_version": SCHEMA_VERSION, "phase": "operation_preflight", "issues": issues,
              "pending_checks": pending, "issue_count": 0, "issues_truncated": False,
              "operation_count": len(operations) if type(operations) is list else 0,
              "complete": False, "all_no_change": False, "write_executed": False}

    def location(index, op):
        return {"operation_index": index, **{key: value[:256] if type(value) is str else None for key, value in {
            "group_key": op.get("action_group_key"), "node_key": op.get("target_node_key") or op.get("anchor_node_key"), "path": op.get("path")}.items()}}

    def issue(index, op, reason, message):
        nonlocal issue_count
        issue_count += 1
        if len(issues) < MAX_ISSUES:
            details = {**location(index, op), "reason_code": reason, "message": message[:256],
                           "expected_shape": operation_shape(op.get("operation")),
                           "refresh_scope": "target_context" if reason in {"old_value_mismatch", "target_not_found", "element_type_mismatch", "ambiguous_target"} else "submitted_operations"}
            details["context_ref"] = diagnostic_context_ref(design, details)
            issues.append(details)

    def defer(index, op, reason):
        if len(pending) < MAX_ISSUES:
            pending.append({**location(index, op), "reason_code": reason})

    if type(operations) is not list or not operations:
        issue(None, {}, "invalid_operation_batch", "Canvas patch requires a nonempty array of operation objects")
    else:
        try:
            validate_json_value(operations)
            if len(json.dumps(operations, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > MAX_OPERATION_BYTES:
                raise CanvasAssertionError("Operation batch exceeds 2 MiB")
        except (CanvasAssertionError, TypeError, ValueError, RecursionError):
            issue(None, {}, "invalid_operation_batch", "Operations require bounded finite JSON data within 2 MiB")
        if not issue_count:
            groups = deepcopy(design.get("actionData", [])) if type(design) is dict and type(design.get("actionData")) is list else []
            aliases, node_aliases, opaque = {}, {}, set()
            failed_groups, failed_nodes, failed_values = set(), {}, []

            def remember_failed(op):
                name = op.get("operation")
                if type(name) is not str:
                    return
                if name == "create_action_group" and _text(op.get("group_alias")):
                    failed_groups.add(op["group_alias"])
                elif name in _ALIAS_CREATORS and _text(op.get("node_alias")):
                    failed_nodes[op["node_alias"]] = op.get("action_group_key")
                elif name in _PARAMETER_OPS and all(_text(op.get(key)) for key in
                        ("action_group_key", "target_node_key", "path")) and "value" in op:
                    failed_values.append(op)

            def depends_on_failed_value(op):
                return any(all(previous.get(key) == op.get(key) for key in
                               ("action_group_key", "target_node_key", "path", "operation"))
                           and same_json(previous.get("item_key"), op.get("item_key"))
                           and same_json(previous["value"], op.get("expected_old_value"))
                           for previous in failed_values)
            all_no_change = True
            for index, op in enumerate(operations):
                if type(op) is not dict:
                    issue(index, {}, "invalid_operation", "Every operation must be an object")
                    all_no_change = False
                    continue
                try:
                    name = op.get("operation")
                    if type(name) is not str or name not in CONTRACT["operations"]:
                        raise _Invalid("invalid_operation", "Unsupported canvas operation")
                    shape = CONTRACT["operations"][name]
                    if set(op).intersection(CONTRACT["forbidden_fields"]) or not set(shape["required"]) <= set(op) or shape["strict_fields"] and set(op) - set(shape["required"]) - set(shape["optional"]):
                        raise _Invalid("invalid_operation", "Operation has missing, forbidden or unsupported fields")
                    for key in shape["required"]:
                        if key not in {"expected_old_value", "value", "item_key"} and not _text(op[key]):
                            raise _Invalid("invalid_operation", "Operation identity fields must be nonblank text")
                    alias = op.get("node_alias")
                    if "node_alias" in op and (name not in _ALIAS_CREATORS or type(alias) is not str or not _ALIAS_RE.fullmatch(alias) or alias in node_aliases):
                        raise _Invalid("invalid_node_alias", "node_alias must be a unique batch-local native insertion alias")
                    if name == "create_action_group":
                        group_alias = op["group_alias"]
                        if not _ALIAS_RE.fullmatch(group_alias) or group_alias in aliases:
                            raise _Invalid("invalid_operation", "group_alias must be unique and valid")
                        if op["action_type"] not in ("js", "csharp") or "\n" in op["group_title"] or "\r" in op["group_title"]:
                            raise _Invalid("invalid_operation", "Group language/title is invalid")
                        if "group_description" in op and type(op["group_description"]) is not str:
                            raise _Invalid("invalid_operation", "group_description must be text")
                        if any(type(g) is dict and g.get("title") == op["group_title"] for g in groups):
                            raise _Invalid("invalid_operation", "Action group title already exists")
                        if "event_contract" in op and op["event_contract"]:
                            event = op["event_contract"]
                            if (type(event) is not dict or set(event) - {"component_type", "event_name"}
                                    or not all(_text(event.get(key)) for key in ("component_type", "event_name"))
                                    or op["action_type"] != "js" or "input_params" in op or "output_params" in op):
                                raise _Invalid("invalid_operation", "event_contract requires a JS group and a component/event identity")
                        else:
                            for section in ("input_params", "output_params"):
                                if section in op:
                                    _declarations(op[section])
                        group_key = "@preflight:" + group_alias
                        while any(type(item) is dict and item.get("key") == group_key for item in groups):
                            group_key += ":"
                        aliases[group_alias] = group_key
                        failed_groups.discard(group_alias)
                        groups.append({"key": group_key, "title": op["group_title"], "actionType": op["action_type"], "data": []})
                        opaque.add(group_key)
                        defer(index, op, "native_generation_required")
                        all_no_change = False
                        continue
                    group_key = op["action_group_key"]
                    if group_key.startswith("@"):
                        if group_key[1:] not in aliases:
                            if group_key[1:] in failed_groups:
                                defer(index, op, "prior_failed_operation_required")
                                remember_failed(op)
                                all_no_change = False
                                continue
                            raise _Invalid("unknown_group_alias", "Unknown or forward action group alias")
                        group_key = aliases[group_key[1:]]
                    matches = [group for group in groups if type(group) is dict and group.get("key") == group_key]
                    if len(matches) != 1:
                        raise _Invalid("ambiguous_target" if matches else "target_not_found", "Operation requires one existing action group")
                    group = matches[0]
                    if type(group.get("data")) is not list:
                        raise _Invalid("invalid_baseline_structure", "Target action group requires a native node array; refresh its context")
                    dependent = group_key in opaque
                    failed_dependency = False
                    for field in ("target_node_key", "anchor_node_key"):
                        key = op.get(field)
                        if not key:
                            continue
                        if type(key) is not str:
                            # Optional falsy references are ignored by the native wrapper.
                            raise _Invalid("invalid_operation", "Target and anchor references must be text")
                        if key.startswith("@"):
                            reference = node_aliases.get(key[1:])
                            if reference is None:
                                if failed_nodes.get(key[1:]) == op["action_group_key"]:
                                    failed_dependency = True
                                    continue
                                raise _Invalid("unknown_node_alias", "Unknown or forward node alias")
                            if reference != group_key:
                                raise _Invalid("node_alias_group_mismatch", "Node alias belongs to a different group")
                            dependent = True
                        elif not dependent:
                            nodes = [node for node in group.get("data", []) if type(node) is dict and node.get("key") == key]
                            if len(nodes) != 1:
                                raise _Invalid("ambiguous_target" if nodes else "target_not_found", "Target/anchor must select one existing node")
                    if name in _PARAMETER_OPS:
                        pointer_tokens(op["path"])
                        _safe_parameter(op["expected_old_value"])
                        _safe_parameter(op["value"])
                        if name == "set_array_item":
                            _item_selector(op["item_key"])
                        if dependent or failed_dependency:
                            defer(index, op, "prior_failed_operation_required" if failed_dependency else "prior_native_result_required")
                            if failed_dependency:
                                remember_failed(op)
                            all_no_change = False
                        else:
                            node = next(node for node in group["data"] if node.get("key") == op["target_node_key"])
                            candidate_node = deepcopy(node)
                            try:
                                unchanged = _parameter_update(candidate_node, op)
                            except _Invalid as exc:
                                if exc.reason == "old_value_mismatch" and depends_on_failed_value(op):
                                    defer(index, op, "prior_failed_operation_required")
                                    remember_failed(op)
                                    all_no_change = False
                                    continue
                                raise
                            node.clear()
                            node.update(candidate_node)
                            all_no_change = unchanged and all_no_change
                    else:
                        if name == "move_block" and op["position"] not in {"before", "after"}:
                            raise _Invalid("invalid_operation", "move_block position must be before or after")
                        if name == "set_event_callback_contract" and group.get("actionType") != "js":
                            raise _Invalid("invalid_operation", "Event callback contract requires a JS group")
                        if name == "bind_component_event":
                            if group.get("actionType") != "js":
                                raise _Invalid("invalid_operation", "Component events require a JS group")
                            if op.get("target_node_key") and (op.get("anchor_node_key") or op.get("position")):
                                raise _Invalid("invalid_operation", "Binding replacement cannot specify insertion position")
                            if op.get("anchor_node_key") and op.get("position") not in ("before", "after"):
                                raise _Invalid("invalid_operation", "Binding anchor requires before/after position")
                            if not op.get("anchor_node_key") and not op.get("target_node_key") and op.get("position") and op["position"] != "append":
                                raise _Invalid("invalid_operation", "Binding position requires an anchor")
                            target_key = op["target_action_group_key"]
                            if target_key.startswith("@"):
                                if target_key[1:] not in aliases:
                                    raise _Invalid("unknown_group_alias", "Unknown or forward callback group alias")
                                target_key = aliases[target_key[1:]]
                            callbacks = [candidate for candidate in groups if type(candidate) is dict and candidate.get("key") == target_key]
                            if len(callbacks) != 1 or callbacks[0].get("actionType") != "js" or target_key == group_key:
                                raise _Invalid("invalid_operation", "Event callback must identify a different JS group")
                        if not failed_dependency:
                            opaque.add(group_key)
                        defer(index, op, "prior_failed_operation_required" if failed_dependency else
                              "prior_native_result_required" if dependent else "native_generation_required")
                        if failed_dependency:
                            remember_failed(op)
                        all_no_change = False
                    if alias:
                        if not failed_dependency:
                            node_aliases[alias] = group_key
                            failed_nodes.pop(alias, None)
                except _Invalid as exc:
                    issue(index, op, exc.reason, str(exc))
                    all_no_change = False
                    remember_failed(op)
            report["all_no_change"] = all_no_change and not issue_count
    report.update(issue_count=issue_count, issues_truncated=issue_count > len(issues), complete=not issue_count and not pending)
    return enrich_diagnostics(design, report)
