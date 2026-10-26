"""Purpose-separated, session-bound references to immutable native old values.

These references carry no approval and are expanded before the existing strict
operation contract is used. No business database or extra state table is used.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from copy import deepcopy
from typing import Any

from .hashing import canonical_json, relation_snapshot_hash, sha256_json
from .operation_preflight import _item_selector, pointer_tokens, same_json


class EditGuardError(ValueError):
    def __init__(self, message: str):
        super().__init__(message)
        self.details = {"phase": "edit_guard", "reason_code": "invalid_edit_guard",
                        "refresh_scope": "target_context"}


def parameter_value(design: dict[str, Any], group_key: str, node_key: str, path: str,
                    item_key: dict[str, Any] | None = None, *, element_key: str | None = None) -> Any:
    groups = [g for g in design.get("actionData", []) if g.get("key") == group_key]
    nodes = [n for n in groups[0].get("data", []) if n.get("key") == node_key] if len(groups) == 1 else []
    if len(nodes) != 1 or element_key is not None and nodes[0].get("elementKey") != element_key:
        raise EditGuardError("Value target must identify exactly one existing native group/node/element")
    value = nodes[0].get("paramsValue")
    try:
        for token in pointer_tokens(path):
            if type(value) is not dict or token not in value:
                raise EditGuardError("Value path must select an existing parameter without array indexes")
            value = value[token]
        if item_key is not None:
            field, selected = _item_selector(item_key)
            matches = [row for row in value if type(row) is dict and field in row and same_json(row[field], selected)] if type(value) is list else []
            if len(matches) != 1:
                raise EditGuardError("Native item selector must identify exactly one existing array row")
            value = matches[0]
        return value
    except ValueError as exc:
        if isinstance(exc, EditGuardError):
            raise
        raise EditGuardError(str(exc)) from exc


def _binding(session: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    return {"purpose": "edit_guard_v1", "conversation_id": conversation_id, "session_id": session["id"],
            "owner_revision": session.get("owner_revision", 0), "backup_id": session["backup_id"],
            "design_id": session["design_id"], "baseline_content_hash": session["baseline"]["content_hash"],
            "relation_sha256": relation_snapshot_hash(session["baseline"].get("relations") or []),
            "compiler_hash": session["compiler_hash"]}


def _identity(operation: dict[str, Any]) -> dict[str, Any]:
    return {"operation": operation.get("operation"), "group_key": operation.get("action_group_key"),
            "node_key": operation.get("target_node_key"), "element_key": operation.get("expected_element_key"),
            "path": operation.get("path"), "item_key": operation.get("item_key")}


def create_guard_ref(session: dict[str, Any], conversation_id: str, operation: dict[str, Any],
                     value: Any, secret: str) -> str:
    payload = {**_binding(session, conversation_id), **_identity(operation), "value_sha256": sha256_json(value)}
    raw = canonical_json(payload).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") + "." + signature


def expand_guard_operations(session: dict[str, Any], conversation_id: str, design: dict[str, Any],
                            operations: Any, secret: str) -> Any:
    if type(operations) is not list:
        return operations  # The existing operation validator owns this error.
    result = deepcopy(operations)
    for operation in result:
        if type(operation) is not dict or "guard_ref" not in operation:
            continue
        if operation.get("operation") not in {"set_parameter", "set_array_item"} or "expected_old_value" in operation:
            raise EditGuardError("Only existing parameter updates may use exactly one of guard_ref and expected_old_value")
        token = operation.pop("guard_ref")
        if type(token) is not str or len(token) > 16384 or token.count(".") != 1:
            raise EditGuardError("guard_ref is malformed")
        encoded, signature = token.split(".")
        if len(signature) != 64 or any(c not in "0123456789abcdef" for c in signature):
            raise EditGuardError("guard_ref signature is malformed")
        try:
            raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            payload = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise EditGuardError("guard_ref is malformed") from exc
        expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise EditGuardError("guard_ref signature is invalid")
        value = parameter_value(design, operation.get("action_group_key"), operation.get("target_node_key"),
                                operation.get("path"), operation.get("item_key"),
                                element_key=operation.get("expected_element_key"))
        bound = {**_binding(session, conversation_id), **_identity(operation), "value_sha256": sha256_json(value)}
        if payload != bound:
            raise EditGuardError("guard_ref belongs to a different session revision, target, path, selector or baseline")
        operation["expected_old_value"] = deepcopy(value)
    return result
