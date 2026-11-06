"""Pure, fail-closed edits of existing native Formily component nodes.

Catalogs are version-bound by the platform adapter. Each component has
``properties`` keyed by component-local JSON Pointer, with JSON Schema type
constraints and an ``operation``. Custom setters require a registered adapter:
``adapters[name] = {contract, complete: True, paths, derived_paths, ...}``.
The property's ``adapter`` selects it (otherwise the operation name is used).
No adapter executes code supplied by a catalog or by an edit request.

Dataset resources contain native id/lastModificationTime/primaryKey/model/name/
group/configModel.queryFields. Dictionary resources contain id/version/items
(native Value/Text). Binding requests use {id, version}; the core constructs the
stored value, rather than accepting a caller-built binding object.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any

from .common import FormError


_OPERATIONS = {"set_property", "set_default", "set_layout", "set_binding", "set_filter"}
_STRUCTURAL = {"properties", "patternProperties", "items", "additionalProperties", "children"}
_IDENTITY = {"name", "id", "type", "x-component", "x-designable-id", "x-index", "x-decorator"}
_EXECUTABLE = {"x-reactions", "x-events", "x-content", "x-linkages"}
_MYSQL_TYPES = {
    **dict.fromkeys(("VARCHAR", "CHAR", "TEXT", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT"), "text"),
    **dict.fromkeys(("INT", "BIT", "TINYINT", "SMALLINT", "MEDIUMINT", "BIGINT", "FLOAT", "DOUBLE", "DECIMAL"), "number"),
    **dict.fromkeys(("DATE", "DATETIME", "TIMESTAMP", "TIME", "YEAR"), "date"),
    "BOOLEAN": "boolean",
}
_MISSING = object()


def _fail(code: str, message: str, **details: Any) -> None:
    raise FormError(code, message, details or None)


def _json(value: Any) -> str:
    def check(item: Any) -> None:
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(k) is str for k in item):
            for child in item.values():
                check(child)
            return
        _fail("invalid_json", "Values must be finite JSON values")
    check(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _same(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


def _parts(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("/") or pointer == "/":
        _fail("invalid_path", "A nonempty component-local JSON Pointer is required")
    raw = pointer[1:].split("/")
    if any(not part or re.search(r"~(?![01])", part) for part in raw):
        _fail("invalid_path", "Invalid JSON Pointer escape or empty segment", path=pointer)
    return [part.replace("~1", "/").replace("~0", "~") for part in raw]


def _safe_path(pointer: str) -> list[str]:
    parts = _parts(pointer)
    if (parts[0] in _IDENTITY or any(p in _STRUCTURAL | _EXECUTABLE for p in parts)
            or (len(parts) == 1 and parts[0] in {"x-component-props", "x-decorator-props"})):
        _fail("protected_path", "Component identity, structure, and executable configuration are read-only", path=pointer)
    if any(part.isdecimal() or part == "-" for part in parts):
        _fail("array_index_forbidden", "Replace an array with a full old-value guard; numeric indexes are forbidden", path=pointer)
    return parts


def _get(node: dict, pointer: str) -> Any:
    value: Any = node
    for part in _parts(pointer):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _put(node: dict, pointer: str, value: Any) -> None:
    parts = _safe_path(pointer)
    current = node
    for part in parts[:-1]:
        if part not in current:
            current[part] = {}
        if not isinstance(current[part], dict):
            _fail("invalid_path", "A property path cannot cross a scalar or array", path=pointer)
        current = current[part]
    current[parts[-1]] = deepcopy(value)


def _created_parents(node: dict, pointer: str) -> list[str]:
    parts = _safe_path(pointer)
    current = node
    created = []
    for index, part in enumerate(parts[:-1]):
        if part not in current:
            created.extend("/" + "/".join(_escape(item) for item in parts[:end]) for end in range(index + 1, len(parts)))
            break
        if not isinstance(current[part], dict):
            _fail("invalid_path", "A property path cannot cross a scalar or array", path=pointer)
        current = current[part]
    return created


def _parent_parts(pointer: str) -> list[str]:
    parts = _parts(pointer)
    if len(parts) == 1 and parts[0] in {"x-component-props", "x-decorator-props"}:
        return parts
    return _safe_path(pointer)


def _remove(node: dict, pointer: str, *, created_container: bool = False) -> None:
    parts = _parent_parts(pointer) if created_container else _safe_path(pointer)
    current = node
    for part in parts[:-1]:
        if not isinstance(current.get(part), dict):
            _fail("restore_guard_mismatch", "A restored property's parent object is missing", path=pointer)
        current = current[part]
    if parts[-1] not in current:
        _fail("restore_guard_mismatch", "The restored property is missing", path=pointer)
    del current[parts[-1]]


def _schema_root(page: dict) -> dict:
    if not isinstance(page, dict):
        _fail("invalid_page", "A native page object is required")
    try:
        root = page["content"]["schema"]
        if not isinstance(root, dict) or not isinstance(root["properties"], dict):
            raise KeyError("properties")
        return root
    except (KeyError, TypeError):
        _fail("invalid_page", "Expected native content.schema.properties")


def _nodes(page: dict) -> list[tuple[dict, dict]]:
    root = _schema_root(page)
    result: list[tuple[dict, dict]] = []

    def visit(node: dict, pointer: str, key: str, parent: str | None) -> None:
        if not isinstance(node, dict):
            _fail("invalid_schema", "Schema nodes must be objects", path=pointer)
        component_type = node.get("x-component")
        if component_type is not None:
            if not isinstance(component_type, str) or not component_type:
                _fail("invalid_schema", "Component types must be nonempty strings", path=pointer)
            info = {"component_id": key, "component_type": component_type, "path": pointer,
                    "parent_component_id": parent, "title": deepcopy(node.get("title"))}
            result.append((info, node))
            parent = key
        properties = node.get("properties", {})
        if not isinstance(properties, dict):
            _fail("invalid_schema", "Schema properties must be an object", path=pointer)
        for child_key, child in properties.items():
            visit(child, f"{pointer}/properties/{_escape(child_key)}", child_key, parent)
        items = node.get("items")
        if isinstance(items, dict):
            visit(items, f"{pointer}/items", f"{key}[]", parent)
        elif isinstance(items, list):
            for index, child in enumerate(items):
                visit(child, f"{pointer}/items/{index}", f"{key}[{index}]", parent)

    for key, node in root["properties"].items():
        visit(node, f"/content/schema/properties/{_escape(key)}", key, None)
    return result


def find_components(page: dict) -> list[dict]:
    """Enumerate native component IDs without altering layout or node order."""
    _json(page)
    return [info for info, _ in _nodes(page)]


def _component(page: dict, component_id: str) -> tuple[dict, dict]:
    matches = [(info, node) for info, node in _nodes(page) if info["component_id"] == component_id]
    if len(matches) != 1:
        _fail("component_not_unique", "The component ID must identify exactly one existing component",
              component_id=component_id, matches=len(matches))
    return matches[0]


def inspect_component(page: dict, component_id: str) -> dict:
    _json(page)
    info, node = _component(page, component_id)
    return {**info, "schema": deepcopy(node)}


def _validate(value: Any, descriptor: dict, path: str) -> None:
    _json(value)
    expected = descriptor.get("type")
    accepted = expected if isinstance(expected, list) else [expected]
    types = {"null": type(None), "boolean": bool, "string": str, "integer": int,
             "number": (int, float), "object": dict, "array": list}
    if not accepted or any(t not in types for t in accepted):
        _fail("unknown_property_contract", "The catalog must contain a supported JSON type", path=path)
    if not any((type(value) in types[t] if isinstance(types[t], tuple) else type(value) is types[t]) for t in accepted):
        _fail("property_type_mismatch", "Property value does not match its native JSON type", path=path, expected=expected)
    if "enum" in descriptor and not any(_same(value, item) for item in descriptor["enum"]):
        _fail("property_enum_mismatch", "Property value is not a native allowed value", path=path)
    if type(value) in (int, float):
        for key, bad in (("minimum", lambda n: value < n), ("maximum", lambda n: value > n)):
            if key in descriptor and bad(descriptor[key]):
                _fail("property_range_mismatch", "Property value exceeds native limits", path=path, limit=key)
    if isinstance(value, str):
        if len(value) < descriptor.get("minLength", 0) or len(value) > descriptor.get("maxLength", float("inf")):
            _fail("property_range_mismatch", "String length exceeds native limits", path=path)
    if isinstance(value, list):
        if len(value) < descriptor.get("minItems", 0) or len(value) > descriptor.get("maxItems", float("inf")):
            _fail("property_range_mismatch", "Array size exceeds native limits", path=path)
        if isinstance(descriptor.get("items"), dict):
            for item in value:
                _validate(item, descriptor["items"], path)
    if isinstance(value, dict):
        properties = descriptor.get("properties", {})
        if any(key not in value for key in descriptor.get("required", [])):
            _fail("property_contract_mismatch", "Required native object properties are missing", path=path)
        if descriptor.get("additionalProperties") is False and any(k not in properties for k in value):
            _fail("property_contract_mismatch", "Unknown object properties are forbidden", path=path)
        for key, child in value.items():
            if key in properties:
                _validate(child, properties[key], f"{path}/{_escape(key)}")


def _closed_contract(descriptor: dict, path: str) -> None:
    """Generic complex values need recursively closed native shape metadata."""
    kinds = descriptor.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    if "object" in kinds:
        if not isinstance(descriptor.get("properties"), dict) or descriptor.get("additionalProperties") is not False:
            _fail("unknown_setter", "Object setters require a closed native property schema", path=path)
        for key, child in descriptor["properties"].items():
            if not isinstance(child, dict):
                _fail("unknown_setter", "Nested native property metadata is incomplete", path=path)
            _closed_contract(child, f"{path}/{_escape(key)}")
    if "array" in kinds:
        if not isinstance(descriptor.get("items"), dict):
            _fail("unknown_setter", "Array setters require native item metadata", path=path)
        _closed_contract(descriptor["items"], path)


def _adapter(component_catalog: dict, descriptor: dict, operation: str, path: str) -> dict:
    key = descriptor.get("adapter", operation)
    adapter = component_catalog.get("adapters", {}).get(key)
    if not isinstance(adapter, dict) or adapter.get("complete") is not True or path not in adapter.get("paths", []):
        _fail("unknown_setter", "A complete native setter adapter is required", path=path)
    return adapter


def _resource(resources: dict | None, collection: str, request: Any) -> dict:
    if not isinstance(request, dict) or set(request) != {"id", "version"} or not isinstance(request["id"], str) or not request["id"]:
        _fail("invalid_reference", "A binding requires exactly an ID and native version")
    collection_data = (resources or {}).get(collection, {})
    resource = collection_data.get(request["id"]) if isinstance(collection_data, dict) else None
    if not isinstance(resource, dict) or resource.get("id") != request["id"]:
        _fail("reference_not_found", "The referenced resource was not read from the platform", resource_id=request["id"])
    version = resource.get("lastModificationTime") if collection == "datasets" else resource.get("version")
    if version is None or not _same(version, request["version"]):
        _fail("reference_version_mismatch", "The referenced resource version changed", resource_id=request["id"])
    return resource


def _dataset_value(resource: dict, previous: Any) -> dict:
    required = ("id", "primaryKey", "model", "name", "lastModificationTime")
    if any(not isinstance(resource.get(key), str) or not resource[key] for key in required):
        _fail("invalid_reference_metadata", "Dataset metadata is incomplete")
    fields = resource.get("configModel", {}).get("queryFields")
    if not isinstance(fields, list) or not fields:
        _fail("invalid_reference_metadata", "Dataset query field metadata is required")
    query_fields = []
    seen = set()
    seen_ids = set()
    seen_values = set()
    for field in fields:
        if not isinstance(field, dict) or any(not isinstance(field.get(k), str) or not field[k] for k in ("id", "name", "tableName", "type")):
            _fail("invalid_reference_metadata", "Dataset fields require IDs, names, tables, and database types")
        db_type = _MYSQL_TYPES.get(field["type"].upper())
        if db_type is None or field["name"] in seen or field["id"] in seen_ids or "." not in field["name"]:
            _fail("invalid_reference_metadata", "Dataset field types/names must match the native setter contract")
        seen.add(field["name"])
        seen_ids.add(field["id"])
        master = field["tableName"] == resource["model"]
        another = field.get("alias") or (field["name"].replace(".", "$", 1) if not master else None)
        result_name = another or field["name"].split(".", 1)[1]
        if not isinstance(result_name, str) or result_name in seen_values:
            _fail("invalid_reference_metadata", "Dataset result field names must be unique strings")
        seen_values.add(result_name)
        query_field = {"ModelKey": field["id"], "FieldKey": field["name"],
                       "FieldName": field["name"].replace(".", "$", 1), "Master": master, "dbType": db_type}
        # JavaScript JSON.stringify omits undefined object members. Preserve that
        # behavior instead of turning absent native metadata into explicit nulls.
        if field.get("columnName") or "description" in field:
            query_field["ColName"] = field.get("columnName") or field.get("description")
        for source, target in (("description", "Describe"), ("columnName", "ModelName"), ("format", "format")):
            if source in field:
                query_field[target] = field[source]
        if another is not None:
            query_field["AnotherName"] = another
        query_fields.append(query_field)
    if not any(f["Master"] and f["FieldName"].split("$", 1)[1] == resource["primaryKey"] for f in query_fields):
        _fail("invalid_reference_metadata", "The dataset primary key must be a query field")
    result = {"value": resource["id"], "primaryKey": resource["primaryKey"], "modelname": resource["model"],
            "isChangeModel": not isinstance(previous, dict) or
            previous.get("lastModificationTime") != resource["lastModificationTime"],
            "sourcetype": "DataCenter", "queryField": query_fields,
            "lastModificationTime": resource["lastModificationTime"], "modelText": resource["name"]}
    if "group" in resource:
        result["selectedGroup"] = resource["group"]
    return result


def _binding(node: dict, path: str, value: Any, adapter: dict, resources: dict | None) -> dict[str, Any]:
    contract = adapter.get("contract")
    if contract in {"native-dataset-v1", "dynamic-list-dataset-v1"}:
        resource = _resource(resources, "datasets", value)
        stored = _dataset_value(resource, _get(node, path))
        modelkey = adapter.get("modelkey_path")
        modelname = adapter.get("modelname_path")
        if not isinstance(modelkey, str) or not isinstance(modelname, str):
            _fail("unknown_setter", "Dataset adapters must declare modelkey/modelname effects")
        updates = {path: stored, modelkey: stored["value"], modelname: stored["modelname"]}
        if contract == "dynamic-list-dataset-v1" and stored["isChangeModel"]:
            target = adapter.get("showconfig_path")
            ids = adapter.get("query_field_ids")
            strategy = adapter.get("query_field_id_strategy")
            if not isinstance(target, str) or (not isinstance(ids, dict) and strategy != "sha256-8-v1"):
                _fail("unknown_setter", "Dynamic list adapters must supply stable field IDs and showconfig path")
            all_list = []
            for field in stored["queryField"]:
                field_id = ids.get(field["FieldKey"]) if isinstance(ids, dict) else hashlib.sha256(
                    _json([resource["id"], resource["lastModificationTime"], field["ModelKey"]]).encode("utf-8")
                ).hexdigest()[:8]
                if not isinstance(field_id, str) or not field_id:
                    _fail("unknown_setter", "Every derived display field needs a platform-generated ID")
                name = field["FieldName"].split("$", 1)[1] if field["Master"] else field["FieldName"]
                display_field = {"SelectText": True, "Value": field.get("AnotherName") or name,
                                 "Width": "100", "YsField": name,
                                 "dbType": field["dbType"], "dbName": field["FieldKey"], "id": field_id}
                for source, target_key in (("ColName", "Text"), ("ModelName", "modelName"), ("format", "format")):
                    if source in field:
                        display_field[target_key] = field[source]
                all_list.append(display_field)
            if len({entry["id"] for entry in all_list}) != len(all_list):
                _fail("invalid_reference_metadata", "Derived field IDs must be unique")
            updates[target] = {"list": [], "modelName": stored["modelname"], "pageSize": 10,
                               "allList": all_list, "storageField": stored["primaryKey"],
                               "selectName": stored["primaryKey"], "showHeader": True,
                               "dataCenterId": stored["value"]}
        return updates
    if contract == "native-dictionary-v1":
        resource = _resource(resources, "dictionaries", value)
        items = resource.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) or "Value" not in item or "Text" not in item for item in items):
            _fail("invalid_reference_metadata", "Dictionary items must use native Value/Text fields")
        target = adapter.get("static_data_path")
        if not isinstance(target, str):
            _fail("unknown_setter", "Dictionary setters must declare their staticDataList effect")
        return {path: resource["id"], target: resource["id"]}
    _fail("unknown_setter", "This native binding setter has no verified implementation", contract=contract)


def _operation_updates(node: dict, op: dict, entry: dict, descriptor: dict, resources: dict | None) -> dict:
    kind, path, value = op["operation"], op["path"], op["value"]
    if kind in {"set_property", "set_layout"}:
        setter_contract = descriptor.get("setter_contract")
        if (descriptor.get("setter") or setter_contract is not None) and setter_contract not in {"scalar-v1", "json-schema-v1"}:
            _fail("unknown_setter", "The property setter is not a supported native setter", path=path)
        if type(value) in (dict, list) and setter_contract != "json-schema-v1":
            _fail("unknown_setter", "Object/array setters need a complete native JSON Schema contract", path=path)
        if setter_contract == "json-schema-v1":
            _closed_contract(descriptor, path)
        _validate(value, descriptor, path)
        if path.endswith("/gridSpan") and (type(value) is not int or not 1 <= value <= 12):
            _fail("property_range_mismatch", "Native grid spans must be integers from 1 through 12", path=path)
        return {path: value}
    adapter = _adapter(entry, descriptor, kind, path)
    requirements = adapter.get("requires", {})
    if not isinstance(requirements, dict) or any(_get(node, required_path) is _MISSING or not _same(_get(node, required_path), required_value)
                                                  for required_path, required_value in requirements.items()):
        _fail("native_setter_precondition", "The native setter is unavailable in this component configuration", path=path)
    contract = adapter.get("contract")
    if kind == "set_binding":
        return _binding(node, path, value, adapter, resources)
    if kind == "set_default" and contract == "native-default-v1":
        if type(value) in (dict, list):
            _fail("unknown_setter", "Complex defaults require a dedicated verified native setter", path=path)
        _validate(value, descriptor, path)
        return {path: value}
    if kind == "set_default" and contract == "native-dictionary-default-v1":
        _validate(value, descriptor, path)
        id_path = adapter.get("dictionary_id_path")
        if not isinstance(id_path, str):
            _fail("unknown_setter", "Dictionary defaults require the native dictionary binding path")
        dictionary_id = _get(node, id_path)
        resource = (resources or {}).get("dictionaries", {}).get(dictionary_id) if isinstance(dictionary_id, str) else None
        if not isinstance(resource, dict) or resource.get("id") != dictionary_id or not resource.get("version"):
            _fail("reference_not_found", "Dictionary defaults require current platform dictionary metadata")
        items = resource.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) or "Value" not in item for item in items):
            _fail("invalid_reference_metadata", "Dictionary item metadata is incomplete")
        selected = value if isinstance(value, list) else ([] if value in (None, "") else [value])
        if any(not any(_same(item, option["Value"]) for option in items) for item in selected):
            _fail("invalid_reference", "Default dictionary values must exist in the bound dictionary")
        return {path: value}
    if kind == "set_filter" and contract == "native-filter-toggle-v1":
        _validate(value, {"type": "boolean"}, path)
        return {path: value}
    _fail("unknown_setter", "This native setter has no verified implementation", path=path, contract=contract)


def apply_operations(page: dict, operations: list[dict], catalog: dict, resources: dict | None = None) -> dict:
    """Return an edited copy and every explicit/derived change; never mutate input.

    Old values are strict JSON comparisons (True is not 1). To configure a
    previously absent whitelisted property, supply expected_old_exists=False
    and expected_old_value=None. Guards are evaluated sequentially in a batch.
    """
    _json(page)
    _json(catalog)
    if resources is not None:
        _json(resources)
    if not isinstance(catalog, dict) or type(catalog.get("schema_version")) is not int or catalog.get("schema_version") != 1 or any(
        not isinstance(catalog.get(k), str) or not catalog[k] for k in ("component_version", "source_fingerprint")
    ) or not isinstance(catalog.get("components"), dict):
        _fail("invalid_catalog", "A version-bound native property catalog is required")
    if not isinstance(operations, list) or not operations:
        _fail("invalid_operations", "At least one structured component operation is required")
    working = deepcopy(page)
    before_components = find_components(page)
    changes: list[dict] = []
    for index, op in enumerate(operations):
        required = {"operation", "component_id", "expected_component_type", "path", "expected_old_value", "value"}
        allowed = required | {"expected_old_exists", "platform"}
        if not isinstance(op, dict) or required - op.keys() or op.keys() - allowed or op.get("operation") not in _OPERATIONS:
            _fail("invalid_operation", "Unsupported or incomplete component operation", operation_index=index)
        _json(op)
        if any(not isinstance(op[k], str) or not op[k] for k in ("component_id", "expected_component_type", "path")):
            _fail("invalid_operation", "Component IDs, types, and paths must be nonempty strings", operation_index=index)
        if type(op.get("expected_old_exists", True)) is not bool:
            _fail("invalid_operation", "expected_old_exists must be boolean", operation_index=index)
        info, node = _component(working, op["component_id"])
        if info["component_type"] != op["expected_component_type"]:
            _fail("component_type_mismatch", "The component type changed", component_id=op["component_id"])
        path = op["path"]
        _safe_path(path)
        entry = catalog["components"].get(info["component_type"])
        descriptor = entry.get("properties", {}).get(path) if isinstance(entry, dict) else None
        if (not isinstance(descriptor, dict) or descriptor.get("writable", True) is not True or descriptor.get("readOnly") is True
                or descriptor.get("editable", True) is not True or descriptor.get("setter_supported", True) is not True
                or descriptor.get("operation") != op["operation"]):
            _fail("property_readonly", "The property is not allowed for this native operation", path=path)
        platform = op.get("platform", catalog.get("platform"))
        if "platform" in op and "platform" in catalog and op["platform"] != catalog["platform"]:
            _fail("platform_mismatch", "An operation cannot override the bound page platform", path=path)
        if "platforms" in descriptor and platform not in descriptor["platforms"]:
            _fail("platform_mismatch", "The property is unavailable on the selected platform", path=path)
        previous = _get(node, path)
        exists = previous is not _MISSING
        if exists != op.get("expected_old_exists", True) or (exists and not _same(previous, op["expected_old_value"])) or (
            not exists and op["expected_old_value"] is not None
        ):
            _fail("old_value_mismatch", "The component property changed or its existence guard is incorrect", path=path)
        updates = _operation_updates(node, op, entry, descriptor, resources)
        adapter = _adapter(entry, descriptor, op["operation"], path) if op["operation"] not in {"set_property", "set_layout"} else {}
        allowed_paths = {path, *adapter.get("derived_paths", [])}
        if set(updates) - allowed_paths:
            _fail("undeclared_derived_change", "A native setter effect was not declared by the catalog", paths=sorted(set(updates) - allowed_paths))
        for target, value in updates.items():
            _safe_path(target)
            target_descriptor = entry.get("properties", {}).get(target)
            if not isinstance(target_descriptor, dict):
                _fail("unknown_property_contract", "Every native setter effect requires a catalog property contract", path=target)
            _validate(value, target_descriptor, target)
            old = _get(node, target)
            if old is not _MISSING and _same(old, value):
                continue
            created_parents = _created_parents(node, target)
            _put(node, target, value)
            changes.append({"operation_index": index, "operation": op["operation"],
                            "component_id": info["component_id"], "component_type": info["component_type"],
                            "path": target, "old_exists": old is not _MISSING,
                            "old_value": None if old is _MISSING else deepcopy(old),
                            "value": deepcopy(value), "derived": target != path,
                            "created_parents": created_parents})
    # A native JSON round trip must retain values, node identities, order, and
    # parent relationships. Only the whitelisted local writes above are possible.
    serialized = _json(working)
    round_trip = json.loads(serialized)
    if not _same(round_trip, working):
        _fail("schema_roundtrip_failed", "Native page serialization changed its structure")
    identity = lambda rows: [(r["component_id"], r["component_type"], r["path"], r["parent_component_id"]) for r in rows]
    if identity(before_components) != identity(find_components(round_trip)):
        _fail("schema_structure_changed", "An operation changed component identity, nesting, or order")
    return {"page": working, "changes": changes, "warnings": [],
            "validation": {"valid": True, "schema_roundtrip": True, "structure_preserved": True,
                           "component_version": catalog["component_version"],
                           "source_fingerprint": catalog["source_fingerprint"]}}


def restore_approved_changes(current_page: dict, approved_changes: list[dict], catalog: dict) -> dict:
    """Invert authenticated saved changes, preserving native identities and model.

    This is an internal helper, not a public edit operation. Its caller MUST
    authenticate the original plan, saved receipt, backup and catalog binding,
    verify current-page continuity, and recheck bound resource versions before
    calling. A caller-provided page replacement is never accepted here.
    """
    _json(current_page)
    _json(approved_changes)
    _json(catalog)
    if (not isinstance(catalog, dict) or type(catalog.get("schema_version")) is not int or catalog.get("schema_version") != 1
            or not isinstance(catalog.get("components"), dict) or any(not isinstance(catalog.get(key), str) or not catalog[key]
                for key in ("component_version", "source_fingerprint"))):
        _fail("invalid_catalog", "A version-bound native property catalog is required")
    if not isinstance(approved_changes, list) or not approved_changes:
        _fail("invalid_restore_changes", "Authenticated saved changes are required")
    working = deepcopy(current_page)
    original_identity = [(row["component_id"], row["component_type"], row["path"], row["parent_component_id"])
                         for row in find_components(current_page)]
    reverse_changes = []
    required = {"operation_index", "operation", "component_id", "component_type", "path", "old_exists", "old_value", "value", "derived"}
    for change in reversed(approved_changes):
        if (not isinstance(change, dict) or required - change.keys() or change.keys() - (required | {"created_parents"})
                or change.get("operation") not in _OPERATIONS or type(change.get("old_exists")) is not bool
                or type(change.get("derived")) is not bool or type(change.get("operation_index")) is not int
                or change["operation_index"] < 0 or any(not isinstance(change.get(key), str) or not change[key]
                    for key in ("component_id", "component_type", "path"))):
            _fail("invalid_restore_changes", "The authenticated saved change shape is invalid")
        if not change["old_exists"] and change["old_value"] is not None:
            _fail("invalid_restore_changes", "An absent prior property cannot have a prior value")
        pointer = change["path"]
        _safe_path(pointer)
        _, node = _component(working, change["component_id"])
        if node["x-component"] != change["component_type"]:
            _fail("component_type_mismatch", "A saved component's type changed before restore")
        entry = catalog["components"].get(change["component_type"])
        descriptor = entry.get("properties", {}).get(pointer) if isinstance(entry, dict) else None
        if not isinstance(descriptor, dict):
            _fail("property_readonly", "A saved property is absent from the bound native catalog", path=pointer)
        current = _get(node, pointer)
        if current is _MISSING or not _same(current, change["value"]):
            _fail("restore_guard_mismatch", "A saved property changed after the approved save", path=pointer)
        parents = change.get("created_parents", [])
        if (not isinstance(parents, list) or not all(isinstance(parent, str) for parent in parents)
                or len(set(parents)) != len(parents) or (parents and change["old_exists"])):
            _fail("invalid_restore_changes", "Saved parent-creation metadata is invalid")
        pointer_parts = _safe_path(pointer)
        for parent in parents:
            if not isinstance(parent, str):
                _fail("invalid_restore_changes", "Saved parent paths must be strings")
            parent_parts = _parent_parts(parent)
            if len(parent_parts) >= len(pointer_parts) or pointer_parts[:len(parent_parts)] != parent_parts:
                _fail("invalid_restore_changes", "A saved parent path does not contain its saved property")
        if change["old_exists"]:
            _put(node, pointer, change["old_value"])
        else:
            _remove(node, pointer)
        # Only containers recorded as created by the signed original operation
        # may be pruned, and only after they become empty during inversion.
        for parent in sorted(parents, key=lambda item: len(_parts(item)), reverse=True):
            parent_value = _get(node, parent)
            if parent_value != {}:
                _fail("restore_guard_mismatch", "A newly created parent container has later contents", path=parent)
            _remove(node, parent, created_container=True)
        reverse_changes.append({"operation_index": change["operation_index"], "operation": change["operation"],
            "component_id": change["component_id"], "component_type": change["component_type"], "path": pointer,
            "old_exists": True, "old_value": deepcopy(current), "value": deepcopy(change["old_value"]),
            "new_exists": change["old_exists"], "derived": change["derived"]})
    round_trip = json.loads(_json(working))
    final_identity = [(row["component_id"], row["component_type"], row["path"], row["parent_component_id"])
                      for row in find_components(round_trip)]
    if not _same(round_trip, working) or original_identity != final_identity:
        _fail("schema_structure_changed", "Restore changed native structure or serialization")
    return {"page": working, "changes": reverse_changes, "warnings": [],
            "validation": {"valid": True, "schema_roundtrip": True, "structure_preserved": True,
                           "component_version": catalog["component_version"], "source_fingerprint": catalog["source_fingerprint"]}}
