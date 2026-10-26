"""Bounded native canvas labels and identifiers from an authorized baseline.

No parameters, generated code, descriptions or source bodies are searched or
returned. Session ownership and version checks belong to the service caller.
"""
from __future__ import annotations

import json
from typing import Any

from .canvas_assertions import CanvasAssertionError, validate_json_value
from .edit_context import EditContextError, _objects, _text, _unique


MAX_LABEL_LENGTH = 256
MAX_DISCOVERY_BYTES = 256 * 1024


class NodeDiscoveryError(ValueError):
    def __init__(self, message: str, *, reason_code: str = "invalid_node_discovery"):
        super().__init__(message)
        self.details = {"phase": "node_discovery", "reason_code": reason_code,
                        "refresh_scope": "node_discovery"}


def _label(value: Any, field: str) -> str | None:
    if value is not None and type(value) is not str:
        raise NodeDiscoveryError(field + " must be text when present")
    return value


def _display(value: str | None) -> str | None:
    return value[:MAX_LABEL_LENGTH] if value is not None else None


def _filter(value: Any, field: str, maximum: int = 512) -> str:
    if type(value) is not str or len(value) > maximum or (value and not value.strip()):
        raise NodeDiscoveryError(field + " must be empty or nonblank text of at most " + str(maximum) + " characters")
    return value


def find_baseline_nodes(design: dict[str, Any], registry: dict[str, Any], *, kind: str = "nodes",
                        query: str = "", group_key: str = "", element_key: str = "",
                        offset: int = 0, limit: int = 30) -> dict[str, Any]:
    """List groups or nodes in canvas order, preserving every exact native key.

Text search uses only group keys/titles and node titles/element keys. Group
search uses group labels; element_key restricts groups to those containing it.
Duplicate labels remain separate rows; duplicate native keys reject the canvas.
"""
    if type(kind) is not str or kind not in {"nodes", "groups"}:
        raise NodeDiscoveryError("kind must be nodes or groups")
    query = _filter(query, "query", 200)
    group_key = _filter(group_key, "group_key")
    element_key = _filter(element_key, "element_key")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise NodeDiscoveryError("Use offset >= 0 and limit between 1 and 100")
    if type(design) is not dict or type(registry) is not dict:
        raise NodeDiscoveryError("design and registry must be objects")

    # Match inspect_edit_context's complete-canvas integrity checks before
    # filtering so a hidden duplicate or invalid node cannot become a target.
    try:
        validate_json_value(design)
        validate_json_value(registry)
        elements = _unique(_objects(registry.get("elements"), "registry.elements"), "registry.elements")
        groups = _unique(_objects(design.get("actionData"), "design.actionData"), "design.actionData")
        _objects(design.get("globalVariables"), "design.globalVariables")
        seen_keys, rows = set(), []
        needle = query.casefold()
        for group_index, (native_group_key, group) in enumerate(groups.items()):
            if group.get("actionType") not in {"js", "csharp"}:
                raise NodeDiscoveryError("Group has an unknown actionType")
            group_title = _label(group.get("title"), "group.title")
            for section in ("inputParams", "outputParams"):
                _objects(group.get(section, []), "group." + section)
            nodes = _objects(group.get("data"), "group.data")
            _unique(nodes, "group.data")
            preceding, node_rows = set(), []
            for index, node in enumerate(nodes):
                node_key = node["key"]
                if node_key in seen_keys:
                    raise NodeDiscoveryError("Canvas contains an ambiguous node key", reason_code="ambiguous_target")
                seen_keys.add(node_key)
                native_element_key = _text(node.get("elementKey"), "node.elementKey")
                if native_element_key not in elements:
                    raise NodeDiscoveryError("Unknown native element", reason_code="unknown_native_element")
                for section in ("input_params", "output_params"):
                    _objects(elements[native_element_key].get(section, []), "native_contract." + section)
                depth = node.get("depth", [])
                if (type(depth) is not list or any(type(key) is not str or key not in preceding for key in depth)
                        or len(set(depth)) != len(depth)):
                    raise NodeDiscoveryError("Node depth must reference unique preceding parents in the same group",
                                             reason_code="invalid_node_depth")
                if "paramsValue" in node:
                    if type(node["paramsValue"]) is not dict:
                        raise NodeDiscoveryError("node.paramsValue must be an object")
                    for section in ("inputParams", "outputParams"):
                        if section in node["paramsValue"] and type(node["paramsValue"][section]) is not dict:
                            raise NodeDiscoveryError("node.paramsValue." + section + " must be an object")
                title = _label(node.get("title"), "node.title")
                preceding.add(node_key)
                if element_key and native_element_key != element_key:
                    continue
                node_rows.append((title, {"group_key": native_group_key, "group_title": _display(group_title),
                    "group_title_truncated": group_title is not None and len(group_title) > MAX_LABEL_LENGTH,
                    "language": group["actionType"], "group_index": group_index, "node_key": node_key,
                    "title": _display(title), "title_truncated": title is not None and len(title) > MAX_LABEL_LENGTH,
                    "element_key": native_element_key, "line": index + 1, "depth": list(depth)}))
            if group_key and native_group_key != group_key:
                continue
            group_labels = (native_group_key, group_title or "")
            if kind == "groups":
                if element_key and not node_rows:
                    continue
                if needle and not any(needle in label.casefold() for label in group_labels):
                    continue
                rows.append({"group_key": native_group_key, "group_title": _display(group_title),
                    "group_title_truncated": group_title is not None and len(group_title) > MAX_LABEL_LENGTH,
                    "language": group["actionType"], "group_index": group_index, "node_count": len(nodes)})
            else:
                for title, row in node_rows:
                    labels = (*group_labels, title or "", row["element_key"])
                    if not needle or any(needle in label.casefold() for label in labels):
                        rows.append(row)
    except (CanvasAssertionError, EditContextError) as exc:
        raise NodeDiscoveryError("Node discovery requires an intact native canvas: " + str(exc)) from exc

    response = {"kind": kind, "items": rows[offset:offset + limit], "total": len(rows),
                "offset": offset, "limit": limit, "next_offset": offset + limit if offset + limit < len(rows) else None,
                "filters": {"query": query, "group_key": group_key, "element_key": element_key}, "read_only": True}
    if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > MAX_DISCOVERY_BYTES:
        raise NodeDiscoveryError("Node discovery exceeds 256 KiB; request a smaller limit",
                                 reason_code="node_discovery_response_limit")
    return response
