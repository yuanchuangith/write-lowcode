"""Read-only native edit evidence assembled from one validated session snapshot."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from .canvas_assertions import CanvasAssertionError, validate_json_value
from .reference_repair import _catalog


class EditContextError(ValueError):
    pass


MAX_CONTEXT_BYTES = 1024 * 1024


def _text(value, name):
    if type(value) is not str or not value.strip() or len(value) > 512:
        raise EditContextError(f"{name} must be a nonblank string of at most 512 characters")
    return value


def _objects(value, name):
    if type(value) is not list or any(type(item) is not dict for item in value):
        raise EditContextError(name + " must be an array of objects")
    return value


def _unique(items, name):
    result = {}
    for item in items:
        key = _text(item.get("key"), name + ".key")
        if key in result:
            raise EditContextError(name + " contains a duplicate key: " + key)
        result[key] = item
    return result


def _summary(node, index):
    return {"node_key": node["key"], "line": index + 1, "title": node.get("title"),
            "element_key": node["elementKey"]}


def _params(node):
    return {"params_value": deepcopy(node.get("paramsValue")), "params_value_present": "paramsValue" in node}


def build_edit_context(design: dict[str, Any], registry: dict[str, Any], targets: list[dict[str, str]]) -> dict[str, Any]:
    """No IO, generation or inference: expose exact parameters and picker scope.

    Caller owns session authorization, baseline/compiler binding and freshness.
    Native function bodies are not present in the registry and remain unparsed.
    """
    if type(targets) is not list or not 1 <= len(targets) <= 10:
        raise EditContextError("targets must contain 1 to 10 exact group_key/node_key objects")
    selected, seen = [], set()
    for target in targets:
        if type(target) is not dict or set(target) != {"group_key", "node_key"}:
            raise EditContextError("Each target must contain only group_key and node_key")
        pair = (_text(target["group_key"], "target.group_key"), _text(target["node_key"], "target.node_key"))
        if pair in seen:
            raise EditContextError("targets contains a duplicate group/node")
        seen.add(pair)
        selected.append(pair)
    if type(design) is not dict or type(registry) is not dict:
        raise EditContextError("design and registry must be objects")
    try:
        validate_json_value(design)
        validate_json_value(registry)
    except CanvasAssertionError as exc:
        raise EditContextError("Edit context requires bounded finite JSON data: " + str(exc)) from exc
    elements = _unique(_objects(registry.get("elements"), "registry.elements"), "registry.elements")
    groups = _unique(_objects(design.get("actionData"), "design.actionData"), "design.actionData")
    _objects(design.get("globalVariables"), "design.globalVariables")
    all_keys, locations = set(), {}
    for group_key, group in groups.items():
        if group.get("actionType") not in ("js", "csharp"):
            raise EditContextError("Group has an unknown actionType: " + group_key)
        for section in ("inputParams", "outputParams"):
            _objects(group.get(section, []), "group." + section)
        nodes = _objects(group.get("data"), "group.data")
        _unique(nodes, "group.data")
        previous_keys = set()
        for index, node in enumerate(nodes):
            node_key = node["key"]
            if node_key in all_keys:
                raise EditContextError("Canvas contains an ambiguous node key: " + node_key)
            all_keys.add(node_key)
            element_key = _text(node.get("elementKey"), "node.elementKey")
            if element_key not in elements:
                raise EditContextError("Unknown native element: " + element_key)
            for section in ("input_params", "output_params"):
                _objects(elements[element_key].get(section, []), "native_contract." + section)
            depth = node.get("depth", [])
            if (type(depth) is not list or any(type(key) is not str or key not in previous_keys for key in depth)
                    or len(set(depth)) != len(depth)):
                raise EditContextError("Node depth must reference unique preceding parents in the same group: " + node_key)
            if "paramsValue" in node:
                if type(node["paramsValue"]) is not dict:
                    raise EditContextError("node.paramsValue must be an object: " + node_key)
                for section in ("inputParams", "outputParams"):
                    if section in node["paramsValue"] and type(node["paramsValue"][section]) is not dict:
                        raise EditContextError("node.paramsValue." + section + " must be an object: " + node_key)
            previous_keys.add(node_key)
            locations[group_key, node_key] = (group, index, node)
    nodes = []
    for pair in selected:
        if pair not in locations:
            raise EditContextError("Target group/node does not exist: " + "/".join(pair))
        group, index, node = locations[pair]
        parents = []
        for parent_key in node.get("depth", []):
            _, parent_index, parent = locations[group["key"], parent_key]
            parents.append({**_summary(parent, parent_index), **_params(parent), "depth": deepcopy(parent.get("depth", []))})
        nodes.append({"group_key": group["key"], "group_title": group.get("title"), "language": group["actionType"],
                      **_summary(node, index), **_params(node), "depth": deepcopy(node.get("depth", [])),
                      "previous": _summary(group["data"][index - 1], index - 1) if index else None,
                      "next": _summary(group["data"][index + 1], index + 1) if index + 1 < len(group["data"]) else None,
                      "parents": parents, "visible_variables": deepcopy(_catalog(design, group, index, elements)),
                      "native_contract": deepcopy(elements[node["elementKey"]]), "source_evidence_required": True,
                      "source_evidence_hint": "Registry definitions are exact; native generation/editor function bodies and their source locations are not parsed. Inspect source evidence when the structured contract is insufficient."})
    result = {"nodes": nodes, "read_only": True, "source_evidence_required": True}
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise EditContextError("Edit context exceeds 1 MiB; reduce targets or inspect a smaller parameter contract")
    return result
