"""Bounded native canvas labels and identifiers from an authorized baseline.

No parameters, generated code, descriptions or source bodies are searched or
returned. Session ownership and version checks belong to the service caller.
"""
from __future__ import annotations

import json
from typing import Any

from .native_catalog import NativeCatalogError, validated_catalog


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

    # Validate the complete canvas even when only one small page is requested.
    try:
        _, groups, _ = validated_catalog(design, registry)
    except NativeCatalogError as exc:
        raise NodeDiscoveryError(str(exc), reason_code=exc.reason_code) from exc
    rows, total = [], 0
    needle = query.casefold()
    for group_index, (native_group_key, group) in enumerate(groups.items()):
        group_title = _label(group.get("title"), "group.title")
        group_labels = (native_group_key, group_title or "")
        selected_group = not group_key or native_group_key == group_key
        contains_element = not element_key
        for index, node in enumerate(group["data"]):
            # Preserve label validation for off-page/filtered nodes, too.
            title = _label(node.get("title"), "node.title")
            native_element_key = node["elementKey"]
            if element_key and native_element_key != element_key:
                continue
            contains_element = True
            if kind != "nodes" or not selected_group:
                continue
            labels = (*group_labels, title or "", native_element_key)
            if needle and not any(needle in label.casefold() for label in labels):
                continue
            if offset <= total < offset + limit:
                rows.append({"group_key": native_group_key, "group_title": _display(group_title),
                    "group_title_truncated": group_title is not None and len(group_title) > MAX_LABEL_LENGTH,
                    "language": group["actionType"], "group_index": group_index, "node_key": node["key"],
                    "title": _display(title), "title_truncated": title is not None and len(title) > MAX_LABEL_LENGTH,
                    "element_key": native_element_key, "line": index + 1, "depth": list(node.get("depth", []))})
            total += 1
        if kind == "groups" and selected_group and contains_element and (
                not needle or any(needle in label.casefold() for label in group_labels)):
            if offset <= total < offset + limit:
                rows.append({"group_key": native_group_key, "group_title": _display(group_title),
                    "group_title_truncated": group_title is not None and len(group_title) > MAX_LABEL_LENGTH,
                    "language": group["actionType"], "group_index": group_index, "node_count": len(group["data"])})
            total += 1
    response = {"kind": kind, "items": rows, "total": total,
                "offset": offset, "limit": limit, "next_offset": offset + limit if offset + limit < total else None,
                "filters": {"query": query, "group_key": group_key, "element_key": element_key}, "read_only": True}
    if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > MAX_DISCOVERY_BYTES:
        raise NodeDiscoveryError("Node discovery exceeds 256 KiB; request a smaller limit",
                                 reason_code="node_discovery_response_limit")
    return response
