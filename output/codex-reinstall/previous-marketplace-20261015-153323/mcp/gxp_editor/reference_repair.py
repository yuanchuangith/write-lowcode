"""Conservative ParamInput reference repair using the native picker scope rules.

Only structured references are edited. Custom expressions and action targets are
never rewritten, and the real compiler must validate the complete result.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .hashing import sha256_json


class ReferenceRepairError(RuntimeError):
    pass


REFERENCE_TYPES = {"inputParam", "outputParam", "globalVariable", "localVariable"}
IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def _walk(value, path=()):
    if isinstance(value, dict):
        if value.get("paramTypes") in REFERENCE_TYPES:
            yield path, value
            return
        for key, child in value.items():
            yield from _walk(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, (*path, index))


def _at(value, path):
    for key in path:
        value = value[key]
    return value


def _type(value):
    # No inferred coercions between object/array, number/string or JS/C# types.
    return str(value or "").strip()


def _candidate(kind, name, dtype, value, code, source, label=None):
    reference = {"paramTypes": kind, "value": value, "code": code,
                 "dataType": _type(dtype), "label": label or name}
    identity = {"reference": reference, "source": source}
    return {"candidate_id": sha256_json(identity), "name": name, "source": source,
            "reference": reference}


def _catalog(design, group, index, registry):
    """Mirror ParamInput.addLocalVariable; require declarations before the use."""
    result = []
    language = group["actionType"]
    for section, kind in (("inputParams", "inputParam"), ("outputParams", "outputParam")):
        for param in group.get(section) or []:
            name, key = str(param.get("title") or ""), str(param.get("key") or "")
            if not name or not key:
                continue
            code = name if kind == "inputParam" else (
                f"outputParams['{name}']" if language == "js" else f'outputParams["{name}"]')
            result.append(_candidate(kind, name, param.get("type"), key, code,
                                     {"kind": kind, "group_key": group["key"], "parameter_key": key}))
    for variable in design.get("globalVariables") or []:
        name = str(variable.get("name") or "")
        if not name:
            continue
        code = f"self.globalVariables['{name}']" if language == "js" else f'this._service.TryGetValue("globalVariables.{name}")'
        result.append(_candidate("globalVariable", name, variable.get("dataType"),
                                 "globalVariable-" + name, code, {"kind": "globalVariable", "name": name}))
    consumer_depth = group["data"][index].get("depth") or []
    for declaration in group["data"][:index]:
        depth = declaration.get("depth") or []
        element = registry.get(declaration.get("elementKey"), {})
        level_marker = element.get("level_marker", element.get("levelMarker", False))
        visible = (declaration["key"] in consumer_depth or
                   (not depth and not level_marker) or
                   (depth and not level_marker and depth[-1] in consumer_depth))
        if not visible:
            continue
        params = declaration.get("paramsValue") or {}
        for field, output in (params.get("outputParams") or {}).items():
            if not isinstance(output, dict) or output.get("paramTypes") != "custom":
                continue
            name = str(output.get("value") or "")
            if not IDENTIFIER.fullmatch(name) or output.get("code") != name:
                continue
            dtype = output.get("dataType")
            # SetVariable.generateActionData explicitly sets this from variableType.
            if declaration.get("elementKey") == "SetVariable" and field == "variableName":
                dtype = (params.get("inputParams") or {}).get("variableType") or dtype
            result.append(_candidate("localVariable", name, dtype, "localVariable-" + name, name,
                                     {"kind": "localVariable", "group_key": group["key"],
                                      "node_key": declaration["key"], "field": field,
                                      "depth": depth}, output.get("label")))
    return result


def _names(reference):
    """Use only exact picker names and exact expressions, never fuzzy matching."""
    kind = reference["paramTypes"]
    value = str(reference.get("value") or "")
    value = value.removeprefix(kind + "-")
    label = str(reference.get("label") or "")
    # Native handleParamTypeChange prepends the category label.
    for prefix in ("输入参数-", "输出参数-", "局部变量-", "全局变量-"):
        label = label.removeprefix(prefix)
    return {item for item in (value, label, str(reference.get("code") or "")) if item}


def scan_references(design: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    elements = {item["key"]: item for item in registry.get("elements") or []}
    issues = []
    for group in design.get("actionData") or []:
        for index, node in enumerate(group.get("data") or []):
            catalog = _catalog(design, group, index, elements)
            for path, reference in _walk(node.get("paramsValue") or {}):
                kind = reference["paramTypes"]
                value = str(reference.get("value") or "")
                if kind == "outputParam" and value == "outputParamObject" and reference.get("code") == "outputParams":
                    continue
                same_source = [c for c in catalog if c["reference"]["paramTypes"] == kind and
                               (c["reference"]["value"] == value or (kind in {"inputParam", "outputParam"} and
                                (c["source"].get("parameter_key") == value.removeprefix(kind + "-") or c["name"] == value.removeprefix(kind + "-"))))]
                if len(same_source) == 1 and reference.get("code") == same_source[0]["reference"]["code"]:
                    continue
                names = _names(reference)
                dtype = _type(reference.get("dataType"))
                allowed = {"outputParam"} if kind == "outputParam" else {"inputParam", "localVariable", "globalVariable"}
                if path and path[0] == "outputParams":
                    allowed &= {"localVariable", "globalVariable", "outputParam"}
                candidates = [deepcopy(c) for c in catalog if c["reference"]["paramTypes"] in allowed and
                              (c["name"] in names or c["reference"]["code"] in names or c in same_source) and
                              (not dtype or not c["reference"]["dataType"] or dtype == c["reference"]["dataType"])]
                # Property selections carry a separate picker contract; never silently discard it.
                automatic = (len(candidates) == 1 and dtype and dtype == candidates[0]["reference"]["dataType"]
                             and not reference.get("objectAttribute") and not reference.get("lang"))
                if reference.get("objectAttribute") or reference.get("lang"):
                    candidates = []
                    automatic = False
                issue = {"group_key": group["key"], "group_title": group.get("title"),
                         "node_key": node["key"], "node_title": node.get("title"),
                         "element_key": node.get("elementKey"), "row": index + 1,
                         "path": list(path), "before": deepcopy(reference),
                         "previous_node_key": group["data"][index - 1]["key"] if index else None,
                         "next_node_key": group["data"][index + 1]["key"] if index + 1 < len(group["data"]) else None,
                         "depth": node.get("depth") or [], "candidates": candidates,
                         "status": "automatic" if automatic else "ambiguous" if candidates else "unresolved"}
                issue["repair_id"] = sha256_json({key: issue[key] for key in ("group_key", "node_key", "path", "before")})
                issues.append(issue)
    return {"issues": issues, "summary": {"total": len(issues), **{
        status: sum(issue["status"] == status for issue in issues)
        for status in ("automatic", "ambiguous", "unresolved")}}}


def inspection_projection(design: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Non-executable diagnostic copy: bypass only the reported variable bindings.

    The native validator still checks group/node keys, action references, element
    coverage, languages and block nesting. This copy is never compiled or saved.
    """
    projection = deepcopy(design)
    nodes = {(g["key"], n["key"]): n for g in projection["actionData"] for n in g["data"]}
    for issue in report["issues"]:
        reference = _at(nodes[(issue["group_key"], issue["node_key"])]["paramsValue"], issue["path"])
        code = reference.get("code")
        if not isinstance(code, str):
            code = ""
        reference.update(paramTypes="custom", value=code, code=code)
    return projection


def build_repair(design: dict[str, Any], report: dict[str, Any], choices: list[dict[str, str]]):
    if not isinstance(choices, list):
        raise ReferenceRepairError("choices must be an array of repair_id/candidate_id objects")
    known = {issue["repair_id"]: issue for issue in report["issues"]}
    selected = {}
    for choice in choices:
        if not isinstance(choice, dict) or set(choice) != {"repair_id", "candidate_id"}:
            raise ReferenceRepairError("Each choice must contain only repair_id and candidate_id")
        repair_id = choice["repair_id"]
        if repair_id not in known or repair_id in selected:
            raise ReferenceRepairError("Unknown or duplicate repair_id")
        if known[repair_id]["status"] == "automatic":
            raise ReferenceRepairError("Automatic repairs do not accept overrides")
        candidate = next((c for c in known[repair_id]["candidates"] if c["candidate_id"] == choice["candidate_id"]), None)
        if candidate is None:
            raise ReferenceRepairError("Candidate is not a visible, compatible source for this reference")
        selected[repair_id] = candidate
    missing = [issue for issue in report["issues"] if issue["status"] != "automatic" and issue["repair_id"] not in selected]
    if missing:
        return {"blocked": True, "unresolved": missing}
    if not known:
        raise ReferenceRepairError("No invalid structured variable references were found")
    candidate_design = deepcopy(design)
    nodes = {(g["key"], n["key"]): n for g in candidate_design["actionData"] for n in g["data"]}
    repaired = []
    affected = set()
    for issue in report["issues"]:
        candidate = selected.get(issue["repair_id"]) or issue["candidates"][0]
        reference = _at(nodes[(issue["group_key"], issue["node_key"])]["paramsValue"], issue["path"])
        # Preserve editor-specific extra properties, replace only the five binding fields.
        reference.update(deepcopy(candidate["reference"]))
        repaired.append({**{key: issue[key] for key in ("repair_id", "group_key", "node_key", "row", "path", "before")},
                         "after": deepcopy(reference), "source": candidate["source"],
                         "candidate_id": candidate["candidate_id"],
                         "selection": "automatic" if issue["status"] == "automatic" else "confirmed"})
        affected.add((issue["group_key"], issue["node_key"]))
    operations = []
    for group in candidate_design["actionData"]:
        for node in group["data"]:
            if (group["key"], node["key"]) in affected:
                operations.append({"operation": "replace_params", "action_group_key": group["key"],
                                   "target_node_key": node["key"],
                                   "input_params": deepcopy(node["paramsValue"].get("inputParams") or {}),
                                   "output_params": deepcopy(node["paramsValue"].get("outputParams") or {})})
    return {"blocked": False, "design": candidate_design, "operations": operations, "repairs": repaired}
