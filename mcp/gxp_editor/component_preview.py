"""Read-only component review derived from the actual before/after canvases."""
from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any

from .hashing import canonical_json


class _PlainDescription(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_description(node: dict[str, Any]) -> str:
    parser = _PlainDescription()
    parser.feed(str(node.get("description") or ""))
    return "".join(parser.parts).strip()


def component_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Ignore regenerated captions; include nesting changes and removed groups."""
    result: list[dict[str, Any]] = []
    old_groups = {g["key"]: g for g in before.get("actionData", [])}

    def names_for(nodes):
        return {item["key"]: item.get("title") or item["elementKey"] for item in nodes}

    def side(group, nodes, index, names):
        node = nodes[index]
        neighbor = lambda i: {"line": i + 1, "name": nodes[i].get("title") or nodes[i]["elementKey"]} if 0 <= i < len(nodes) else None
        return {"group": group.get("title") or group["key"], "group_key": group["key"],
                "parents": [names.get(key, key) for key in node.get("depth") or []],
                "line": index + 1, "node": node, "previous": neighbor(index - 1), "next": neighbor(index + 1)}

    def group_side(group):
        if not group:
            return None
        return {"group": group.get("title") or group["key"], "group_key": group["key"],
                "line": None, "previous": None, "next": None,
                "node": {"key": "__group_metadata__", "elementKey": "actionGroup",
                         "title": "动作分组：" + (group.get("title") or group["key"]),
                         "description": "动作分组及回调参数配置", "paramsValue": {"inputParams": {
                             "分组名称": group.get("title"), "动作类型": group.get("actionType"),
                             "分组说明": group.get("description") or "",
                             "输入参数": group.get("inputParams") or [], "输出参数": group.get("outputParams") or []}}}}

    for group in after.get("actionData", []):
        previous_group = old_groups.pop(group["key"], {})
        previous_meta, current_meta = group_side(previous_group), group_side(group)
        if canonical_json(previous_meta) != canonical_json(current_meta):
            result.append({"kind": "修改" if previous_group else "新增", "before": previous_meta, "after": current_meta})
        old_nodes, new_nodes = previous_group.get("data", []), group.get("data", [])
        old_names, new_names = names_for(old_nodes), names_for(new_nodes)
        old_by_key = {node["key"]: index for index, node in enumerate(old_nodes)}
        common = set(old_by_key) & {node["key"] for node in new_nodes}
        old_order = [node["key"] for node in old_nodes if node["key"] in common]
        new_order = [node["key"] for node in new_nodes if node["key"] in common]
        moved = {key for i, key in enumerate(new_order) if key != old_order[i]}
        for index, node in enumerate(new_nodes):
            old_index = old_by_key.pop(node["key"], None)
            if old_index is None:
                kind = "新增"
            else:
                old = old_nodes[old_index]
                fields = ("elementKey", "paramsValue")
                changed = (canonical_json({key: old[key] for key in fields if key in old})
                           != canonical_json({key: node[key] for key in fields if key in node})) or (old.get("depth") or []) != (node.get("depth") or [])
                if not changed and node["key"] not in moved:
                    continue
                kind = "修改并移动" if changed and node["key"] in moved else "修改" if changed else "移动"
            old_side = side(previous_group, old_nodes, old_index, old_names) if old_index is not None else None
            new_side = side(group, new_nodes, index, new_names)
            result.append({"kind": kind, "before": old_side, "after": new_side})
        for index in old_by_key.values():
            result.append({"kind": "删除", "before": side(previous_group, old_nodes, index, old_names), "after": None})
    for group in old_groups.values():
        result.append({"kind": "删除", "before": group_side(group), "after": None})
        names = names_for(group.get("data", []))
        for index in range(len(group.get("data", []))):
            result.append({"kind": "删除", "before": side(group, group["data"], index, names), "after": None})
    if canonical_json(before.get("globalVariables")) != canonical_json(after.get("globalVariables")):
        def variables(data):
            return {"group": "全局变量", "line": None, "node": {"title": "全局变量", "elementKey": "globalVariables",
                    "paramsValue": {"inputParams": {"globalVariables": data.get("globalVariables", [])}}}, "previous": None, "next": None}
        result.append({"kind": "修改", "before": variables(before), "after": variables(after)})
    return result


def canvas_node_locations(data: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Stable identities and real scope, shared by direct and indirect review."""
    locations = {}
    for group in data.get("actionData", []):
        nodes = group.get("data", [])
        names = {node["key"]: node.get("title") or node["elementKey"] for node in nodes}
        for index, node in enumerate(nodes):
            neighbor = lambda i: {"line": i + 1, "name": names[nodes[i]["key"]]} if 0 <= i < len(nodes) else None
            locations[(group["key"], node["key"])] = {
                "group": group.get("title") or group["key"], "group_key": group["key"], "line": index + 1,
                "node": node, "parents": [names.get(key, key) for key in node.get("depth") or []],
                "previous": neighbor(index - 1), "next": neighbor(index + 1),
            }
    return locations


def control_flow_impacts(before: dict[str, Any], after: dict[str, Any], *, changes=None,
                         before_locations=None, after_locations=None) -> list[dict[str, Any]]:
    """Show scope effects separately from actual node edits; never infer execution."""
    direct = component_changes(before, after) if changes is None else changes
    controllers = {}
    control_elements = {"If", "IfCondition", "ElseIfCondition", "Else", "ForLoop", "ForEachArray",
                        "ForEachObject", "ForEachDynamicArray", "WhileLoop", "ExitAction"}
    for item in direct:
        old, new = item.get("before"), item.get("after")
        side = new or old
        node = side["node"]
        if node.get("elementKey") not in control_elements:
            continue
        old_inputs = ((old or {}).get("node", {}).get("paramsValue") or {}).get("inputParams") or {}
        new_inputs = ((new or {}).get("node", {}).get("paramsValue") or {}).get("inputParams") or {}
        if (old and new and canonical_json(old_inputs) == canonical_json(new_inputs)
                and old["node"].get("elementKey") == new["node"].get("elementKey")
                and (old["node"].get("depth") or []) == (new["node"].get("depth") or [])):
            continue
        controllers[(side["group_key"], node["key"])] = {
            "group_key": side["group_key"], "node_key": node["key"], "title": node.get("title") or node["elementKey"],
            "before_condition": old_inputs.get("condition", old_inputs),
            "after_condition": new_inputs.get("condition", new_inputs),
            "field_path": "inputParams.condition" if "condition" in old_inputs or "condition" in new_inputs else "inputParams",
            "potential": node.get("elementKey") not in {"If", "IfCondition", "ElseIfCondition", "Else"},
        }
    if not controllers:
        return []
    # Native Else/ElseIf both close the previous branch (endMarker) and
    # open their own (levelMarker). Their depth therefore ends in the previous
    # branch key; children only contain the new branch key. Follow that chain.
    def dependencies(data):
        result = {}
        for group in data.get("actionData", []):
            earlier_exits = []
            branches = {}
            for node in group.get("data", []):
                depth = node.get("depth") or []
                if node.get("elementKey") in {"ElseIfCondition", "Else"} and depth:
                    previous_branch = depth[-1]
                    branches[node["key"]] = [previous_branch, *branches.get(previous_branch, [])]
                scope = list(dict.fromkeys(key for parent in depth for key in (parent, *branches.get(parent, []))))
                result[(group["key"], node["key"])] = list(dict.fromkeys([*scope, *earlier_exits]))
                if node.get("elementKey") == "ExitAction" and (group["key"], node["key"]) in controllers:
                    earlier_exits.append(node["key"])
        return result
    old_dependencies, new_dependencies = dependencies(before), dependencies(after)
    previous = canvas_node_locations(before) if before_locations is None else before_locations
    current = canvas_node_locations(after) if after_locations is None else after_locations
    result = []
    for identity in dict.fromkeys([*current, *previous]):
        old, new = previous.get(identity), current.get(identity)
        if (new or old)["node"].get("elementKey", "").endswith("End"):
            continue
        parent_keys = dict.fromkeys([*old_dependencies.get(identity, []), *new_dependencies.get(identity, [])])
        affected_by = [controllers[(identity[0], key)] for key in parent_keys if (identity[0], key) in controllers]
        if affected_by:
            kind = "可能受控制流影响" if any(item["potential"] for item in affected_by) else "受条件影响"
            result.append({"kind": kind, "group_key": identity[0], "node_key": identity[1],
                           "before": old, "after": new, "affected_by": affected_by})
    return result


def parameter_text(value: Any) -> str:
    if isinstance(value, dict) and "paramTypes" in value:
        titles = {"label": "显示名称", "paramTypes": "取值方式", "value": "配置值", "code": "完整表达式", "dataType": "数据类型"}
        lines = [f"{titles[key]}：{_text(value[key])}" for key in titles if key in value]
        extra = {k: v for k, v in value.items() if k not in titles}
        if extra:
            lines.append("其它配置：" + _text(extra))
        return "\n".join(lines)
    return _text(value)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def parameter_rows(change: dict[str, Any], registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Use verified registry titles when available; keep unknown field keys intact."""
    rows = []
    old = (change.get("before") or {}).get("node", {})
    new = (change.get("after") or {}).get("node", {})
    for section, heading, metadata_key in (("inputParams", "输入参数", "input_params"), ("outputParams", "输出参数", "output_params")):
        previous = (old.get("paramsValue") or {}).get(section) or {}
        current = (new.get("paramsValue") or {}).get(section) or {}
        old_meta = {p["key"]: p for p in registry.get(old.get("elementKey"), {}).get(metadata_key, [])}
        new_meta = {p["key"]: p for p in registry.get(new.get("elementKey"), {}).get(metadata_key, [])}
        keys = dict.fromkeys([*new_meta, *old_meta, *current, *previous])
        for key in keys:
            if key not in previous and key not in current:
                continue
            old_title = old_meta.get(key, {}).get("title") or key
            new_title = new_meta.get(key, {}).get("title") or key
            rows.append({"section": section, "heading": heading, "key": key,
                         "before_title": old_title, "after_title": new_title,
                         "required": bool(new_meta.get(key, old_meta.get(key, {})).get("required")),
                         "before": parameter_text(previous[key]) if key in previous else "未配置",
                         "after": parameter_text(current[key]) if key in current else "未配置",
                         "changed": (key in previous) != (key in current)
                         or canonical_json(previous.get(key)) != canonical_json(current.get(key))})
    return rows
