"""Read-only native edit evidence assembled from one validated session snapshot."""
from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any

from .canvas_assertions import CanvasAssertionError, validate_json_value
from .reference_repair import _catalog
from .operation_preflight import SCHEMA_VERSION, UNSAFE, atomic_parameters, operation_shape, same_json
from .value_transport import value_summary


class EditContextError(ValueError):
    def __init__(self, message, *, group_key=None, node_key=None, reason_code="invalid_edit_context"):
        super().__init__(message)
        self.details = {"phase": "edit_context", "reason_code": reason_code,
                        "group_key": group_key[:256] if type(group_key) is str else None,
                        "node_key": node_key[:256] if type(node_key) is str else None,
                        "refresh_scope": "target_context"}


MAX_CONTEXT_BYTES = 1024 * 1024
OPERATION_PAGE_SIZE = 64
SUMMARY_OPERATION_PAGE_SIZE = 8


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


def _pointer(parts):
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def _operation_templates(group, node):
    """Yield incomplete safe shapes, referring to existing evidence instead of copying values."""
    params = node.get("paramsValue")
    if type(params) is not dict:
        return
    atomic = atomic_parameters(node["elementKey"])

    def row_identity(value):
        if type(value) is str and value.strip():
            return ("string", value)
        if type(value) in (int, float) and math.isfinite(value) and value == int(value) and abs(value) <= 9007199254740991:
            return ("number", value)
        return None

    def walk(value, parts):
        path = _pointer(parts)
        template = {"operation": "set_parameter", "action_group_key": group["key"],
                    "target_node_key": node["key"], "expected_element_key": node["elementKey"], "path": path}
        yield {"template": template, "complete": False, "required_inputs": ["expected_old_value", "value"],
               "expected_old_value_ref": {"node_key": node["key"], "field": "params_value", "path": path},
               "replacement_kind": "whole_array" if type(value) is list else "whole_paraminput" if parts in atomic else "existing_value",
               "preserve_record_keys": type(value) is dict}
        if type(value) is list:
            # Arrays cannot be traversed by indexes. Advertise only existing unique native selectors.
            for field in ("id", "key"):
                counts = {}
                for row in value:
                    candidate = row.get(field) if type(row) is dict else None
                    identity = row_identity(candidate)
                    if identity is not None:
                        counts[identity] = counts.get(identity, 0) + 1
                for row in value:
                    candidate = row.get(field) if type(row) is dict else None
                    identity = row_identity(candidate)
                    if identity is None or counts[identity] != 1:
                        continue
                    selector = {"field": field, "value": candidate}
                    yield {"template": {**template, "operation": "set_array_item", "item_key": selector},
                           "complete": False, "required_inputs": ["expected_old_value", "value"],
                           "expected_old_value_ref": {"node_key": node["key"], "field": "params_value", "path": path, "item_key": selector},
                           "replacement_kind": "whole_array_item", "preserve_native_id_and_key": True,
                           "preserve_record_keys": True}
        elif type(value) is dict and parts not in atomic:
            for key, item in value.items():
                if key not in UNSAFE:
                    yield from walk(item, parts + [key])

    for section in ("inputParams", "outputParams"):
        for key, value in params.get(section, {}).items():
            if key not in UNSAFE:
                yield from walk(value, [section, key])


def _compact_guard_values(node, templates):
    """Return exact page guards without resending unrelated node evidence."""
    values = []
    params = node.get("paramsValue")
    for row in templates:
        reference = row["expected_old_value_ref"]
        path = reference["path"]
        value = params
        for token in path[1:].split("/"):
            value = value[token.replace("~1", "/").replace("~0", "~")]
        selector = reference.get("item_key")
        if selector is not None:
            value = next(item for item in value if type(item) is dict and selector["field"] in item
                         and same_json(item[selector["field"]], selector["value"]))
        values.append({"path": path, **({"item_key": deepcopy(selector)} if selector is not None else {}),
                       "value": deepcopy(value)})
        row["expected_old_value_ref"] = {"node_key": node["key"], "field": "parameter_guards",
                                          "guard_index": len(values) - 1}
    return values


def build_edit_context(design: dict[str, Any], registry: dict[str, Any], targets: list[dict[str, str]], *,
                       operation_cursor: int = 0, include_context: bool | None = None,
                       response_detail: str = "full", parameter_paths: list[str] | None = None) -> dict[str, Any]:
    """No IO, generation or inference: expose exact parameters and picker scope.

    Caller owns session authorization, baseline/compiler binding and freshness.
    Native function bodies are not present in the registry and remain unparsed.
    """
    if type(operation_cursor) is not int or operation_cursor < 0:
        raise EditContextError("operation_cursor must be a nonnegative integer from the preceding context page")
    if include_context is not None and type(include_context) is not bool:
        raise EditContextError("include_context must be a boolean when provided")
    if response_detail not in {"summary", "full"}:
        raise EditContextError("response_detail must be summary or full")
    if parameter_paths is not None and (type(parameter_paths) is not list or not 1 <= len(parameter_paths) <= 32
            or any(type(path) is not str or not path.startswith("/") or len(path) > 2048 for path in parameter_paths)
            or len(set(parameter_paths)) != len(parameter_paths)):
        raise EditContextError("parameter_paths must contain 1 to 32 unique JSON parameter pointers")
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
                raise EditContextError("Canvas contains an ambiguous node key: " + node_key,
                                       group_key=group_key, node_key=node_key, reason_code="ambiguous_target")
            all_keys.add(node_key)
            element_key = _text(node.get("elementKey"), "node.elementKey")
            if element_key not in elements:
                raise EditContextError("Unknown native element: " + element_key,
                                       group_key=group_key, node_key=node_key, reason_code="unknown_native_element")
            for section in ("input_params", "output_params"):
                _objects(elements[element_key].get(section, []), "native_contract." + section)
            depth = node.get("depth", [])
            if (type(depth) is not list or any(type(key) is not str or key not in previous_keys for key in depth)
                    or len(set(depth)) != len(depth)):
                raise EditContextError("Node depth must reference unique preceding parents in the same group: " + node_key,
                                       group_key=group_key, node_key=node_key, reason_code="invalid_node_depth")
            if "paramsValue" in node:
                if type(node["paramsValue"]) is not dict:
                    raise EditContextError("node.paramsValue must be an object: " + node_key,
                                           group_key=group_key, node_key=node_key, reason_code="invalid_parameter_value")
                for section in ("inputParams", "outputParams"):
                    if section in node["paramsValue"] and type(node["paramsValue"][section]) is not dict:
                        raise EditContextError("node.paramsValue." + section + " must be an object: " + node_key,
                                               group_key=group_key, node_key=node_key, reason_code="invalid_parameter_value")
            previous_keys.add(node_key)
            locations[group_key, node_key] = (group, index, node)
    nodes = []
    summary = response_detail == "summary" and include_context is None
    compact = not include_context if include_context is not None else operation_cursor > 0
    page_size = SUMMARY_OPERATION_PAGE_SIZE if summary else OPERATION_PAGE_SIZE
    template_index, displayed_templates = 0, 0
    for pair in selected:
        if pair not in locations:
            raise EditContextError("Target group/node does not exist: " + "/".join(pair),
                                   group_key=pair[0], node_key=pair[1], reason_code="target_not_found")
        group, index, node = locations[pair]
        parents = []
        if not compact and not summary:
            for parent_key in node.get("depth", []):
                _, parent_index, parent = locations[group["key"], parent_key]
                parents.append({**_summary(parent, parent_index), **_params(parent), "depth": deepcopy(parent.get("depth", []))})
        templates, found_paths = [], set()
        for template in _operation_templates(group, node):
            path = template["template"]["path"]
            if parameter_paths is not None and path not in parameter_paths:
                continue
            found_paths.add(path)
            if operation_cursor <= template_index < operation_cursor + page_size:
                templates.append(template)
                displayed_templates += 1
            template_index += 1
        if parameter_paths is not None and set(parameter_paths) - found_paths:
            raise EditContextError("parameter_paths must select existing editable parameter paths for every target",
                                   group_key=group["key"], node_key=node["key"])
        native = elements[node["elementKey"]]
        insertion_available = native.get("level_marker") is True and native.get("end_marker") is not True
        edit_options = {"schema_version": SCHEMA_VERSION, "parameter_operations": templates,
                        "atomic_parameter_paths": [_pointer(parts) for parts in atomic_parameters(node["elementKey"])],
                        "array_indexes_allowed": False, "source_evidence_required": True,
                        "insert_in_block": {"available": insertion_available}}
        if insertion_available:
            edit_options["insert_in_block"].update(template={"operation": "insert_in_block", "action_group_key": group["key"], "target_node_key": node["key"]},
                complete=False, required_inputs=["element_key"], native_validation_required=True)
        if summary:
            from .edit_guards import parameter_value
            parameters = []
            for template in templates:
                operation = template["template"]
                value = parameter_value(design, group["key"], node["key"], operation["path"], operation.get("item_key"))
                parameters.append({"path": operation["path"],
                    **({"item_key": deepcopy(operation["item_key"])} if "item_key" in operation else {}),
                    **value_summary(value), "read_value": {"tool": "read_edit_value", "arguments": {
                        "group_key": group["key"], "node_key": node["key"], "path": operation["path"],
                        **({"item_key": deepcopy(operation["item_key"])} if "item_key" in operation else {})}}})
            evidence = {"parameters": parameters, "params_value_present": "paramsValue" in node,
                "parents": [_summary(locations[group["key"], key][2], locations[group["key"], key][1])
                            for key in node.get("depth", [])],
                "native_contract_summary": {"key": native["key"], "name": native.get("name"),
                    "input_params_count": len(native.get("input_params", [])),
                    "output_params_count": len(native.get("output_params", []))},
                "evidence_omitted": ["params_value", "parent_parameters", "visible_variables", "native_contract"],
                "full_context": {"tool": "inspect_edit_context", "arguments": {
                    "targets": [{"group_key": group["key"], "node_key": node["key"]}], "response_detail": "full"}}}
        else:
            evidence = ({"parameter_guards": _compact_guard_values(node, templates),
                     "evidence_omitted": ["params_value", "parents", "visible_variables", "native_contract"],
                     "full_evidence_operation_cursor": 0} if compact else {
                         **_params(node), "parents": parents,
                         "visible_variables": deepcopy(_catalog(design, group, index, elements)),
                          "native_contract": deepcopy(native)})
        nodes.append({"group_key": group["key"], "group_title": group.get("title"), "language": group["actionType"],
                      **_summary(node, index), **evidence, "depth": deepcopy(node.get("depth", [])),
                      "previous": _summary(group["data"][index - 1], index - 1) if index else None,
                      "next": _summary(group["data"][index + 1], index + 1) if index + 1 < len(group["data"]) else None,
                      "edit_options": edit_options, "source_evidence_required": True,
                      "source_evidence_hint": "Registry definitions are exact; native generation/editor function bodies and their source locations are not parsed. Inspect source evidence when the structured contract is insufficient."})
    if operation_cursor > template_index:
        raise EditContextError("operation_cursor is outside this context; restart at operation_cursor=0 with the same targets")
    next_cursor = operation_cursor + displayed_templates if operation_cursor + displayed_templates < template_index else None
    result = {"nodes": nodes, "read_only": True, "source_evidence_required": True,
              "context_detail": "summary" if summary else "compact_guards" if compact else "full_context",
              "omitted_fields": ["params_value", "parents", "visible_variables", "native_contract", "operation_contracts"] if compact or summary else [],
              **({} if compact or summary else {"operation_contracts": {name: operation_shape(name) for name in ("set_parameter", "set_array_item", "insert_in_block")}}),
              "operation_page": {"cursor": operation_cursor, "page_size": page_size,
                                  "evidence_mode": "summary" if summary else "compact_guards" if compact else "full_context",
                                 "returned": displayed_templates, "total": template_index, "complete": next_cursor is None,
                                 "next_operation_cursor": next_cursor,
                                  "continuation": "Repeat with identical targets, parameter_paths and detail at next_operation_cursor. Require unchanged context_binding; restart if it changes. Summary uses signed guards; read_edit_value fetches needed values. Explicit include_context retains the legacy full/compact projection."},
              "next_operation_cursor": next_cursor}
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > MAX_CONTEXT_BYTES:
        error = EditContextError("Edit context exceeds 1 MiB; reduce targets or inspect a smaller parameter contract",
                                 reason_code="edit_context_response_limit")
        error.details.update(context_detail=result["context_detail"], max_output_bytes=MAX_CONTEXT_BYTES,
                             next_step="No parameter or guard was truncated. Retry with fewer targets; for a single oversized value use readonly inspect_action with a focused value_path/value_offset. Request include_context=false to omit unrelated context.")
        raise error
    return result
