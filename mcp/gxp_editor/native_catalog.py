"""One complete canvas integrity pass, retaining only requested node locations."""
from __future__ import annotations

from .canvas_assertions import CanvasAssertionError, validate_json_value
from .telemetry import timed


class NativeCatalogError(ValueError):
    def __init__(self, message, *, group_key=None, node_key=None, reason_code="invalid_edit_context"):
        super().__init__(message)
        self.group_key, self.node_key, self.reason_code = group_key, node_key, reason_code


def _text(value, name):
    if type(value) is not str or not value.strip() or len(value) > 512:
        raise NativeCatalogError(f"{name} must be a nonblank string of at most 512 characters")
    return value


def _objects(value, name):
    if type(value) is not list or any(type(item) is not dict for item in value):
        raise NativeCatalogError(name + " must be an array of objects")
    return value


def _unique(items, name):
    result = {}
    for item in items:
        key = _text(item.get("key"), name + ".key")
        if key in result:
            raise NativeCatalogError(name + " contains a duplicate key: " + key)
        result[key] = item
    return result


@timed("canvas.integrity_catalog")
def validated_catalog(design, registry, *, selected=()):
    """Never stop at a page boundary: hidden malformed nodes must still fail.

    Native objects are borrowed synchronously; no values or catalog are cached
    across requests. Locations are allocated only for explicitly selected pairs.
    """
    if type(design) is not dict or type(registry) is not dict:
        raise NativeCatalogError("design and registry must be objects")
    try:
        validate_json_value(design)
        validate_json_value(registry)
    except CanvasAssertionError as exc:
        raise NativeCatalogError("Edit context requires bounded finite JSON data: " + str(exc)) from exc
    elements = _unique(_objects(registry.get("elements"), "registry.elements"), "registry.elements")
    groups = _unique(_objects(design.get("actionData"), "design.actionData"), "design.actionData")
    _objects(design.get("globalVariables"), "design.globalVariables")
    wanted, locations, all_keys, checked_contracts = set(selected), {}, set(), set()
    for group_key, group in groups.items():
        if group.get("actionType") not in ("js", "csharp"):
            raise NativeCatalogError("Group has an unknown actionType: " + group_key)
        for section in ("inputParams", "outputParams"):
            _objects(group.get(section, []), "group." + section)
        nodes = _objects(group.get("data"), "group.data")
        _unique(nodes, "group.data")
        preceding = set()
        for index, node in enumerate(nodes):
            key = node["key"]
            if key in all_keys:
                raise NativeCatalogError("Canvas contains an ambiguous node key: " + key,
                    group_key=group_key, node_key=key, reason_code="ambiguous_target")
            all_keys.add(key)
            element_key = _text(node.get("elementKey"), "node.elementKey")
            if element_key not in elements:
                raise NativeCatalogError("Unknown native element: " + element_key,
                    group_key=group_key, node_key=key, reason_code="unknown_native_element")
            if element_key not in checked_contracts:
                for section in ("input_params", "output_params"):
                    _objects(elements[element_key].get(section, []), "native_contract." + section)
                checked_contracts.add(element_key)
            depth = node.get("depth", [])
            if (type(depth) is not list or any(type(parent) is not str or parent not in preceding for parent in depth)
                    or len(set(depth)) != len(depth)):
                raise NativeCatalogError("Node depth must reference unique preceding parents in the same group: " + key,
                    group_key=group_key, node_key=key, reason_code="invalid_node_depth")
            if "paramsValue" in node:
                if type(node["paramsValue"]) is not dict:
                    raise NativeCatalogError("node.paramsValue must be an object: " + key,
                        group_key=group_key, node_key=key, reason_code="invalid_parameter_value")
                for section in ("inputParams", "outputParams"):
                    if section in node["paramsValue"] and type(node["paramsValue"][section]) is not dict:
                        raise NativeCatalogError("node.paramsValue." + section + " must be an object: " + key,
                            group_key=group_key, node_key=key, reason_code="invalid_parameter_value")
            preceding.add(key)
            if (group_key, key) in wanted:
                locations[group_key, key] = (group, index, node)
    return elements, groups, locations
