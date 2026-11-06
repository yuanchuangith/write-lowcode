"""Extract a closed editing catalog from a JSON export of native propsSchema.

The exporter accepts already resolved runtime metadata, never evaluates TS/JS,
and strips only Formily ``type: void`` presentation groups. Native data-object
groups and dotted Formily property names retain their data paths. Unsupported
setters, missing types, executable reactions, and ambiguous mappings stay visible
in the catalog with an explicit read-only reason.

Usage (local files only)::
    python -m gxp_form_editor.catalog native-export.json catalog.json \
        --component-version 1.2.3 --platform web

Input is {componentType: propsSchema} or {componentType: {designerProps:
{propsSchema: ...}}}. For native function setters the read-only capture may
serialize the function's verified export identity as {"nativeSetter": "Name"};
missing/minified identities remain unsupported. The complete input file is
SHA256-bound as source_fingerprint by the CLI.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from .common import FormError, canonical, digest
from .schema import _json, _safe_path


_SCALAR_SETTERS = {"Input", "Input.TextArea", "Switch", "NumberPicker", "Select", "Radio.Group", "MultilingualInput"}
_ROOT_PROPERTIES = {"title", "description", "required", "default", "x-pattern", "x-display", "x-disabled", "x-read-only", "readOnly"}
_NATIVE_TYPES = {"string", "boolean", "number", "integer", "object", "array", "null"}
_LIMITS = ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems")
_UNSUPPORTED_CONSTRAINTS = {"$ref", "$namespace", "pattern", "format", "multipleOf", "exclusiveMinimum", "exclusiveMaximum",
                            "oneOf", "anyOf", "allOf", "not", "if", "then", "else", "uniqueItems", "const"}
_DEFAULT_PATHS = {
    "EformInput": {"defaultValueType", "systemDefaultValue", "customValue"},
    "EformDynamicList": {"defaulttype", "systemDefaultValue", "default", "defaultValue"},
}


def _error(message: str) -> None:
    raise FormError("invalid_native_export", message)


def _setter(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"nativeSetter"} and isinstance(value["nativeSetter"], str):
        return value["nativeSetter"]
    return None


def _presentational(reactions: Any) -> bool:
    if reactions in (None, [], {}):
        return True
    if isinstance(reactions, list):
        return all(_presentational(reaction) for reaction in reactions)
    if not isinstance(reactions, dict) or set(reactions) - {"dependencies", "when", "fulfill", "otherwise"}:
        return False
    for branch in ("fulfill", "otherwise"):
        state = reactions.get(branch, {})
        if not isinstance(state, dict) or set(state) - {"state"}:
            return False
        visibility = state.get("state", {})
        if not isinstance(visibility, dict) or set(visibility) - {"visible", "hidden"}:
            return False
    return True


def _dictionary_reactions(reactions: Any) -> bool:
    """Recognize only the source-reviewed static-list settings reactions."""
    if _presentational(reactions):
        return True
    if isinstance(reactions, list):
        return all(_dictionary_reactions(item) for item in reactions)
    if not isinstance(reactions, dict) or set(reactions) - {"dependencies", "when", "fulfill", "otherwise"}:
        return False
    if reactions.get("dependencies") not in ([".dataType"], [".dataGroupId", ".selectMode", ".dataType"]):
        return False
    if "when" in reactions and reactions["when"] not in {"{{$deps[0] === 'system'}}", "{{$deps[2] === 'system'}}"}:
        return False
    for branch in ("fulfill", "otherwise"):
        effect = reactions.get(branch, {})
        if not isinstance(effect, dict) or set(effect) - {"schema"}:
            return False
        schema = effect.get("schema", {})
        if not isinstance(schema, dict) or set(schema) - {"x-component", "x-component-props", "x-decorator-props",
                                                       "x-component-props.dataGroupId", "x-component-props.mode", "x-value"}:
            return False
        if "x-value" in schema and schema["x-value"] != "":
            return False
        if "x-component" in schema and _setter(schema["x-component"]) not in {"SelectGroup", "ApiQuery", "Input"}:
            return False
    return True


def _data_effect(reaction: Any) -> bool:
    if isinstance(reaction, list):
        return any(_data_effect(item) for item in reaction)
    if not isinstance(reaction, dict):
        return False
    if any(key in reaction for key in ("run", "value", "x-value", "enum")):
        return True
    return any(_data_effect(item) for item in reaction.values())


def _dependencies(reactions: Any) -> list[str]:
    if isinstance(reactions, list):
        return [dependency for reaction in reactions for dependency in _dependencies(reaction)]
    if not isinstance(reactions, dict) or not _data_effect(reactions):
        return []
    dependencies = reactions.get("dependencies", [])
    return [item for item in dependencies if isinstance(item, str)] if isinstance(dependencies, list) else []


def _unwrap(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    if "properties" in raw:
        return raw
    props = raw.get("designerProps", {}).get("propsSchema") if isinstance(raw.get("designerProps"), dict) else None
    if isinstance(props, dict):
        return props
    if isinstance(raw.get("propsSchema"), dict):
        return raw["propsSchema"]
    behavior = raw.get("Behavior")
    if isinstance(behavior, dict):
        return _unwrap(behavior)
    return None


def _path(parts: list[str]) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def _keys(key: str) -> list[str] | None:
    # Formily dotted paths are supported only for unambiguous simple segments.
    # Bracket expressions/selectors require the actual Formily path runtime.
    if not isinstance(key, str) or not key or any(char in key for char in "[]*()"):
        return None
    parts = key.split(".")
    return parts if all(parts) else None


def _type_contract(native: dict, setter: str | None) -> dict:
    result = {key: deepcopy(native[key]) for key in ("type", *_LIMITS) if key in native}
    options = native.get("x-component-props", {})
    if isinstance(options, dict):
        if "min" in options:
            result["minimum"] = options["min"]
        if "max" in options:
            result["maximum"] = options["max"]
        if result.get("type") == "number" and options.get("precision") == 0:
            result["type"] = "integer"
    if "enum" in native:
        enum = native["enum"]
        if isinstance(enum, list):
            result["enum"] = [deepcopy(item["value"]) if isinstance(item, dict) and "value" in item else deepcopy(item) for item in enum]
    if setter == "StatusInput" and native.get("type") == "object":
        # The native exported StatusInput ValueObj has exactly these two fields.
        result.update({"properties": {"status": {"type": "boolean"}, "value": {"type": "string"}},
                       "required": ["status", "value"], "additionalProperties": False,
                       "setter_contract": "json-schema-v1"})
    return result


def extract_native_catalog(native_components: dict, component_version: str, source_fingerprint: str, platform: str) -> dict:
    """Derive native scalar/default/layout capabilities without hand-written paths.

    Known custom bindings are selected by component type, exact stored path, and
    exported setter identity together. A function lost during JSON serialization
    never becomes writable merely because it has a familiar property name.
    """
    _json(native_components)
    if not isinstance(native_components, dict) or not native_components:
        _error("A nonempty component-to-native-propsSchema export is required")
    if not isinstance(component_version, str) or not component_version or not isinstance(source_fingerprint, str) or not source_fingerprint:
        _error("Native component version and source fingerprint are required")
    if platform not in {"web", "wap"}:
        _error("The exported native platform must be web or wap")
    output = {"schema_version": 1, "component_version": component_version, "source_fingerprint": source_fingerprint,
              "native_export_sha256": digest(native_components), "platform": platform, "components": {}, "warnings": []}
    for component_type, raw in native_components.items():
        if not isinstance(component_type, str) or not component_type:
            _error("Every native component needs a nonempty type")
        schema = _unwrap(raw)
        entry = {"properties": {}, "adapters": {}, "native_props_schema_sha256": digest(schema) if schema is not None else None}
        native_by_path: dict[str, dict] = {}
        inherited_blocks: dict[str, str | None] = {}
        schema_addresses: dict[str, str] = {}
        output["components"][component_type] = entry
        if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
            output["warnings"].append({"component_type": component_type, "reason": "missing_resolved_props_schema"})
            continue

        def visit(native: dict, key: str, storage: list[str], address: list[str], area: str, inherited_reason: str | None = None) -> None:
            if not isinstance(native, dict):
                return
            area_here = key if not storage and key in {"attribute", "style", "senior"} else area
            reason = inherited_reason
            if not _presentational(native.get("x-reactions")):
                reason = reason or "unsupported_native_reaction"
            native_keys = _keys(key)
            if native_keys is None:
                output["warnings"].append({"component_type": component_type, "native_path": _path(address), "reason": "ambiguous_native_data_path"})
                return
            children = native.get("properties")
            if native.get("type") == "void":
                if any(part.startswith("x-component-props") or part.startswith("x-decorator-props") for part in native_keys):
                    reason = reason or "ambiguous_void_storage_group"
                if isinstance(children, dict):
                    for child_key, child in children.items():
                        visit(child, child_key, storage, [*address, "properties", child_key], area_here, reason)
                return
            next_storage = [*storage, *native_keys]
            setter = _setter(native.get("x-component"))
            if isinstance(children, dict) and native.get("type") == "object" and setter is None:
                for child_key, child in children.items():
                    visit(child, child_key, next_storage, [*address, "properties", child_key], area_here, reason)
                return
            pointer = _path(next_storage)
            native_by_path[pointer] = native
            inherited_blocks[pointer] = inherited_reason
            schema_addresses[".".join(part for part in address if part != "properties")] = pointer
            descriptor = _type_contract(native, setter)
            descriptor.update({"operation": "set_layout" if area_here == "style" else "set_property",
                               "setter": setter, "native_schema_path": _path(address), "writable": True})
            if "platform" in native:
                descriptor["platforms"] = [native["platform"]]
                if native["platform"] != platform:
                    reason = reason or "native_platform_mismatch"
            if next_storage[0] not in _ROOT_PROPERTIES | {"x-component-props", "x-decorator-props"}:
                reason = reason or "unrecognized_native_storage_root"
            try:
                _safe_path(pointer)
            except FormError:
                reason = reason or "protected_native_path"
            kinds = descriptor.get("type")
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if not kinds or any(kind not in _NATIVE_TYPES for kind in kinds):
                reason = reason or "missing_native_json_type"
            if any(constraint in native for constraint in _UNSUPPORTED_CONSTRAINTS):
                reason = reason or "unsupported_native_constraint"
            if native.get("readOnly") is True or native.get("x-disabled") is True or native.get("x-read-only") is True:
                reason = reason or "native_readonly"
            known_scalar = setter in _SCALAR_SETTERS and all(kind in {"string", "number", "integer", "boolean", "null"} for kind in kinds)
            known_status = setter == "StatusInput" and native.get("type") == "object"
            if known_scalar:
                descriptor["setter_contract"] = "scalar-v1"
            elif not known_status:
                reason = reason or "unsupported_native_setter"
            leaf = next_storage[-1]
            if (next_storage == ["default"] or (next_storage[:1] == ["x-component-props"] and len(next_storage) == 2
                    and leaf in _DEFAULT_PATHS.get(component_type, set()))) and known_scalar:
                descriptor["operation"] = "set_default"
                adapter_key = "default:" + pointer
                descriptor["adapter"] = adapter_key
                entry["adapters"][adapter_key] = {"contract": "native-default-v1", "complete": True, "paths": [pointer], "derived_paths": []}
            if component_type == "FormGrid.GridColumn" and pointer == "/x-component-props/gridSpan":
                descriptor["operation"] = "set_layout"
            if pointer == "/x-component-props/dataFilter" and component_type in {"AdvancedTables", "FormTables", "GxpSmartTables"} and native.get("type") == "boolean" and setter == "Switch":
                descriptor["operation"] = "set_filter"
                descriptor["adapter"] = "filter-toggle"
                entry["adapters"]["filter-toggle"] = {"contract": "native-filter-toggle-v1", "complete": True, "paths": [pointer], "derived_paths": []}
            if pointer in entry["properties"]:
                existing = entry["properties"][pointer]
                # Native schemas repeat some style properties. Equal contracts
                # can be deduplicated; conflicting sources are never writable.
                ignored = {"native_schema_path"}
                if {k: v for k, v in existing.items() if k not in ignored} != {k: v for k, v in descriptor.items() if k not in ignored}:
                    existing.update({"writable": False, "readonly_reason": "conflicting_native_property_definitions"})
                return
            if reason:
                descriptor.update({"writable": False, "readonly_reason": reason})
            entry["properties"][pointer] = descriptor

        for native_key, native_node in schema["properties"].items():
            visit(native_node, native_key, [], ["properties", native_key], "attribute")

        # Changing a scalar can trigger another property's native value/enum
        # reaction. Such source fields also need an implemented native adapter;
        # merely checking the source field's own reactions would miss effects.
        for target, native in native_by_path.items():
            for dependency in _dependencies(native.get("x-reactions")):
                if dependency.startswith(".") and not dependency.startswith(".."):
                    suffix = _keys(dependency[1:])
                    source = _path([*target[1:].split("/")[:-1], *suffix]) if suffix else None
                else:
                    source = schema_addresses.get(dependency)
                if source in entry["properties"]:
                    entry["properties"][source].update({"writable": False, "readonly_reason": "unsupported_dependent_native_reaction"})

        # Fixed, source-reviewed binding adapters. Function setter names and all
        # needed stored properties must be present in the captured metadata.
        def enable_binding(pointer: str, setter: str, contract: str, effects: list[str], **options: Any) -> None:
            descriptor = entry["properties"].get(pointer)
            if not isinstance(descriptor, dict) or descriptor.get("setter") != setter or any(path not in entry["properties"] for path in effects):
                return
            if descriptor.get("readonly_reason") not in {"unsupported_native_setter", "unsupported_native_reaction", None}:
                return
            if inherited_blocks.get(pointer):
                return
            reactions = native_by_path[pointer].get("x-reactions")
            if not (_dictionary_reactions(reactions) if contract == "native-dictionary-v1" else _presentational(reactions)):
                return
            descriptor.update({"operation": "set_binding", "adapter": "binding:" + pointer, "writable": True})
            descriptor.pop("readonly_reason", None)
            entry["adapters"][descriptor["adapter"]] = {"contract": contract, "complete": True,
                        "paths": [pointer], "derived_paths": effects, **options}

        if component_type == "EformDynamicList":
            enable_binding("/x-component-props/dataCenter", "DataCenter", "dynamic-list-dataset-v1",
                           ["/x-component-props/modelkey", "/x-component-props/modelname", "/x-component-props/showconfig"],
                           modelkey_path="/x-component-props/modelkey", modelname_path="/x-component-props/modelname",
                           showconfig_path="/x-component-props/showconfig", query_field_id_strategy="sha256-8-v1")
            if "binding:/x-component-props/dataCenter" in entry["adapters"]:
                for derived in ("/x-component-props/modelkey", "/x-component-props/modelname", "/x-component-props/showconfig"):
                    entry["properties"][derived].update({"writable": False, "readonly_reason": "native_binding_derived_property"})
        if component_type == "EformStaticList":
            enable_binding("/x-component-props/dataGroupId", "SelectGroup", "native-dictionary-v1", ["/x-component-props/staticDataList"],
                           static_data_path="/x-component-props/staticDataList", requires={"/x-component-props/dataType": "system"})
            if "binding:/x-component-props/dataGroupId" in entry["adapters"]:
                static_data = entry["properties"]["/x-component-props/staticDataList"]
                # SelectGroup.onChange explicitly sets staticDataList=e (the
                # native group Key), despite its settings-schema array type.
                # Expose this source-reviewed setter effect transparently.
                static_data.update({"native_declared_type": static_data.get("type"), "type": "string",
                                    "writable": False, "readonly_reason": "native_binding_derived_property"})
            defaults = entry["properties"].get("/x-component-props/defaultValue")
            if (defaults and defaults.get("setter") == "DictDataSelect" and defaults.get("type") == "array"
                    and defaults.get("readonly_reason") in {"unsupported_native_setter", "unsupported_native_reaction", None}
                    and not inherited_blocks.get("/x-component-props/defaultValue")
                    and _dictionary_reactions(native_by_path["/x-component-props/defaultValue"].get("x-reactions"))):
                # Item value types must come from the actual dictionary read;
                # this setter only accepts a whole guarded native value array.
                defaults.update({"operation": "set_default", "adapter": "dictionary-default", "writable": True})
                defaults.pop("readonly_reason", None)
                entry["adapters"]["dictionary-default"] = {"contract": "native-dictionary-default-v1", "complete": True,
                    "paths": ["/x-component-props/defaultValue"], "dictionary_id_path": "/x-component-props/dataGroupId",
                    "requires": {"/x-component-props/dataType": "system"}}
    output["available"] = any(d.get("writable") is True for entry in output["components"].values() for d in entry["properties"].values())
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert a local resolved native propsSchema JSON export into a guarded form-edit catalog")
    parser.add_argument("native_export", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--component-version", required=True)
    parser.add_argument("--platform", required=True, choices=("web", "wap"))
    args = parser.parse_args(argv)
    source = args.native_export.read_bytes()
    native = json.loads(source.decode("utf-8-sig"))
    result = extract_native_catalog(native, args.component_version, hashlib.sha256(source).hexdigest(), args.platform)
    if args.output.resolve() == args.native_export.resolve():
        parser.error("The catalog output must differ from the native export")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(result) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
