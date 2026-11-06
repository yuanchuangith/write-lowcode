"""Guarded whole-operation revision and bounded reads of a saved plan.

This module does not validate native node semantics or authorize planning. The
caller must first validate plan ownership, scope and baseline, then run the
ordinary native preflight and business review for the resulting operations.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

from .hashing import sha256_json
from .planning_limits import MAX_OPERATION_BYTES, MAX_REVIEW_BYTES
from .value_transport import field_value, value_page


def _operations(operations: Any) -> list[dict]:
    if type(operations) is not list or any(type(operation) is not dict for operation in operations):
        raise ValueError("operations must be a list of complete JSON objects")
    try:
        encoded = json.dumps(operations, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("operations must contain finite JSON values") from error
    pending = [operations]
    while pending:
        value = pending.pop()
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise ValueError("operation objects must use string keys")
            pending.extend(value.values())
        elif type(value) is list:
            pending.extend(value)
        elif value is not None and type(value) not in (str, bool, int, float):
            raise ValueError("operations must contain finite JSON values")
    if len(encoded.encode("utf-8")) > MAX_OPERATION_BYTES:
        raise ValueError("operations exceed the 2 MiB planning limit")
    return operations


def revise_operations(operations: list, replacements: list | None = None) -> list:
    """Replace guarded list entries without changing their order or the input.

    Each replacement has exactly ``index``, ``expected_operation_sha256`` and
    ``operation``. Guards bind the complete original operation, never a summary.
    Empty replacements are useful when only the business review is changing.
    """
    _operations(operations)
    if replacements is None:
        replacements = []
    if type(replacements) is not list:
        raise ValueError("replacements must be a list or null")
    seen = set()
    for replacement in replacements:
        if type(replacement) is not dict or set(replacement) != {"index", "expected_operation_sha256", "operation"}:
            raise ValueError("each replacement requires exactly index, expected_operation_sha256 and operation")
        index = replacement["index"]
        if type(index) is not int or index < 0 or index >= len(operations):
            raise ValueError("replacement index is outside the saved operation list")
        if index in seen:
            raise ValueError("replacement indexes must be unique")
        seen.add(index)
        expected = replacement["expected_operation_sha256"]
        if type(expected) is not str or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise ValueError("expected_operation_sha256 must be a complete SHA-256 digest")
        if expected != sha256_json(operations[index]):
            raise ValueError("the complete saved operation no longer matches its expected hash")
        _operations([replacement["operation"]])
    revised = deepcopy(operations)
    for replacement in replacements:
        revised[replacement["index"]] = deepcopy(replacement["operation"])
    _operations(revised)
    return revised


def revise_review_spec(base_spec: dict, update: dict) -> dict:
    """Upsert complete requirements/assertions; retain all other expectations.

    This helper never removes a row, changes scope policy, or revises scenarios.
    It does not establish authorization for changed expectations: the ordinary
    review checkpoint/source-evidence checks must still run downstream.
    """
    from .logic_review import normalize_review_spec

    if type(base_spec) is not dict or type(update) is not dict:
        raise ValueError("base_spec and update must be JSON objects")
    allowed = {"revision", "schema_version", "requirements", "canvas_assertions", "change_source_text"}
    if set(update) - allowed or "revision" not in update:
        raise ValueError("review update requires revision and only supported upsert fields")
    if (type(update["revision"]) is not str or not update["revision"].strip()
            or len(update["revision"]) > 128):
        raise ValueError("review update revision must be nonblank text of at most 128 characters")
    if "schema_version" in update and (type(update["schema_version"]) is not int or update["schema_version"] != 1):
        raise ValueError("review update schema_version must be the integer 1")
    try:
        size = len(json.dumps(update, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("review update must contain finite JSON values") from error
    if size > MAX_REVIEW_BYTES:
        raise ValueError("review update exceeds the 2 MiB review limit")
    revised = deepcopy(normalize_review_spec(base_spec))
    revised["revision"] = update["revision"]
    if "change_source_text" in update:
        revised["change_source_text"] = deepcopy(update["change_source_text"])
    for section in ("requirements", "canvas_assertions"):
        if section not in update:
            continue
        updates = update[section]
        if type(updates) is not list:
            raise ValueError("review update sections must be arrays of complete rows")
        seen = set()
        for row in updates:
            if (type(row) is not dict or type(row.get("id")) is not str or not row["id"].strip()
                    or len(row["id"]) > 128 or row["id"] in seen):
                raise ValueError("review update rows need unique nonblank identifiers of at most 128 characters")
            seen.add(row["id"])
        original = revised.get(section, [])
        positions = {row["id"]: index for index, row in enumerate(original)}
        for row in updates:
            if row["id"] in positions:
                original[positions[row["id"]]] = deepcopy(row)
            else:
                positions[row["id"]] = len(original)
                original.append(deepcopy(row))
        revised[section] = original
    return deepcopy(normalize_review_spec(revised))


_IDENTITY_FIELDS = (
    "action_group_key", "target_node_key", "anchor_node_key", "expected_element_key", "element_key",
    "target_action_group_key", "group_alias", "node_alias", "path",
)


def _summary(operation: dict, index: int, *, minimal=False) -> dict:
    name = operation.get("operation")
    result = {"index": index, "operation_sha256": sha256_json(operation),
              "operation_name": name if type(name) is str and len(name) <= 128 else None,
              "operation_complete": False}
    if not minimal:
        omitted = []
        for key in _IDENTITY_FIELDS:
            value = operation.get(key)
            if type(value) is str and len(value) <= 256:
                result[key] = value
            elif key in operation:
                omitted.append(key)
        if omitted:
            result["omitted_identity_fields"] = omitted
    return result


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def operation_page(operations: list, plan_id: str, offset: int = 0, limit: int = 10,
                   detail: str = "summary", field_path: str | None = None, value_offset: int = 0,
                   max_output_bytes: int = 16384, conversation_id: str = "") -> dict:
    """Read exact operations only on request; large values use canonical pages.

    Row offsets select original operation indexes. ``field_path`` is relative
    to a full row, so ``/operation`` reads a whole operation and
    ``/operation/input_params`` reads that field. Value offsets count Unicode
    code points. A fragment never carries an ``operation`` key or claims to be
    a complete operation; assemble and verify its canonical value hash first.
    """
    _operations(operations)
    if type(plan_id) is not str or not plan_id.strip() or len(plan_id) > 128:
        raise ValueError("plan_id must be a nonblank plan identity of at most 128 characters")
    if type(conversation_id) is not str or len(conversation_id) > 128:
        raise ValueError("conversation_id must be at most 128 characters")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Use offset >= 0 and limit between 1 and 20")
    if detail not in {"summary", "full"}:
        raise ValueError("detail must be summary or full")
    if type(value_offset) is not int or value_offset < 0:
        raise ValueError("value_offset must be a nonnegative character offset")
    if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 65536:
        raise ValueError("max_output_bytes must be between 1024 and 65536")
    if field_path is not None and type(field_path) is not str:
        raise ValueError("field_path must select an operation using a JSON pointer")

    def next_read(index: int, *, path: str = "/operation", value_position: int = 0) -> dict:
        return {"tool": "read_plan_review", "arguments": {
            "plan_id": plan_id, "conversation_id": conversation_id, "section": "operations",
            "offset": index, "limit": 1, "detail": "full", "field_path": path,
            "value_offset": value_position, "max_output_bytes": max_output_bytes}}

    if field_path:
        if field_path != "/operation" and not field_path.startswith("/operation/"):
            raise ValueError("field_path must select /operation or one of its fields")
        if offset >= len(operations):
            raise ValueError("the selected saved operation does not exist")
        row = {**_summary(operations[offset], offset), "operation": operations[offset]}
        value = field_value(row, field_path)
        metadata = {"section": "operations", "plan_id": plan_id, "row_offset": offset,
                    "operation_sha256": row["operation_sha256"], "field_path": field_path,
                    "detail": "full"}
        overhead = _size(metadata) + _size({"next_read": next_read(offset, path=field_path, value_position=MAX_OPERATION_BYTES)}) + 128
        if max_output_bytes - overhead < 1024:
            raise ValueError("byte budget is too small for operation value paging; request a larger max_output_bytes")
        page = value_page(value, offset=value_offset, max_output_bytes=max_output_bytes - overhead)
        result = {**metadata, **deepcopy(page), "max_output_bytes": max_output_bytes}
        if not page["complete"]:
            result["next_read"] = (next_read(offset, path=field_path, value_position=page["next_offset"])
                                   if page["next_offset"] is not None else None)
            result["assembly_required"] = True
        if _size(result) > max_output_bytes:
            raise ValueError("byte budget is too small for this operation value; request a larger max_output_bytes")
        return result
    if value_offset:
        raise ValueError("value_offset requires field_path")

    def response(items: list) -> dict:
        continuation = offset + len(items)
        return {"section": "operations", "plan_id": plan_id, "items": items,
                "offset": offset, "total": len(operations), "detail": detail,
                "next_offset": continuation if continuation < len(operations) else None,
                "max_output_bytes": max_output_bytes}

    items = []
    for index in range(offset, min(len(operations), offset + limit)):
        operation = operations[index]
        summary = _summary(operation, index)
        row = ({**summary, "operation": deepcopy(operation), "operation_complete": True}
               if detail == "full" else summary)
        if _size(response([*items, row])) > max_output_bytes - 256:
            if items:
                break
            if detail == "summary":
                row = _summary(operation, index, minimal=True)
                if _size(response([row])) > max_output_bytes:
                    raise ValueError("byte budget is too small for operation metadata")
                items.append(row)
                break
            result = {**response([summary]), "complete": False,
                      "oversized_item": {"index": index, "operation_bytes": _size(operation)},
                      "next_read": next_read(index),
                      "hint": "Read /operation with value_offset; assemble fragments and verify the complete value hash before replacing an operation."}
            if _size(result) > max_output_bytes:
                result["items"] = [_summary(operation, index, minimal=True)]
            if _size(result) > max_output_bytes:
                raise ValueError("byte budget is too small for operation continuation; request a larger max_output_bytes")
            return result
        items.append(row)
    return response(items)
