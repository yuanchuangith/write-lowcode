"""Display models for configuration flow and observed field state; no evaluation."""
from __future__ import annotations

import json
import re
from copy import deepcopy

from .component_preview import canvas_node_locations, control_flow_impacts, plain_description
from .logic_review_model import operation_rule, rule_path

MISSING = object()
UNDEFINED = object()


def same(left, right):
    if left is MISSING or right is MISSING or left is UNDEFINED or right is UNDEFINED:
        return left is right
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(right, sort_keys=True, ensure_ascii=False)


def value_text(value):
    if value is MISSING:
        return "未观测"
    if value is UNDEFINED:
        return "undefined（未定义）"
    if value is None:
        return "空值 null"
    if isinstance(value, bool):
        return "是 true" if value else "否 false"
    if isinstance(value, str):
        return value if value else '空字符串 ""'
    if isinstance(value, list):
        return f"{len(value)} 条数据 · " + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def expression(value):
    if isinstance(value, dict):
        if value.get("code") is not None:
            return expression(value["code"])
        return expression(value["value"]) if "value" in value else ""
    return value_text(value)


def parameter(value):
    """Labels and executable identity are both visible, even for identical labels."""
    if isinstance(value, dict):
        code = expression(value)
        title = str(value.get("label") or value.get("name") or code or "未配置")
        attribute = value.get("objectAttribute")
        return title, code + (" · " + str(attribute) if attribute else "")
    return value_text(value), ""


def field_reference(value):
    match = re.fullmatch(r'''\s*inbiz\(\s*(["'])([^"']+)\1\s*\)(?:\.value)?\s*''', expression(value))
    return match.group(2) if match else None


def condition_text(condition):
    if not isinstance(condition, dict):
        return expression(condition)
    if "Filters" in condition:
        joiner = " 且 " if str(condition.get("Logic", "And")).lower() == "and" else " 或 "
        filters = condition["Filters"]
        if not isinstance(filters, list):
            return value_text(condition)
        return joiner.join("(" + condition_text(item) + ")" if isinstance(item, dict) and "Filters" in item else condition_text(item)
                           for item in filters) or "未配置条件"
    if "target" in condition or "Field" in condition:
        target = condition.get("target", condition.get("Field"))
        code = expression(target)
        op = str(condition.get("equalTo") or condition.get("Operator") or "")
        expected = condition.get("value", MISSING)
        # Only shorten exact, known expressions. All originals remain in evidence.
        mode = re.fullmatch(r'''\s*inbiz\.getPageMode\(\)\s*===?\s*(["'])(add|edit)\1\s*''', code)
        if op == "Equal" and expected is True:
            if mode:
                return "页面模式 = " + {"add": "新增", "edit": "编辑"}[mode.group(2)]
            if code == "true":
                return "始终成立"
        ops = {"Equal": "=", "NotEqual": "≠", "EqualNull": "为空", "NotEqualNull": "不为空",
               "GreaterThan": ">", "LessThan": "<", "GreaterThanOrEqual": "≥", "LessThanOrEqual": "≤"}
        # SelectData stores a parameter descriptor separately from its legacy
        # value. Keep the executable expression visible, without evaluating it.
        configured = condition.get("ParamInput", expected)
        return code + " " + ops.get(op, op) + ("" if op in {"EqualNull", "NotEqualNull"} else " " + expression(configured))
    return expression(condition)


ROLES = ("数据来源", "执行条件", "处理动作", "流向目标")
BRANCHES = {"If", "IfCondition", "ElseIfCondition", "Else"}


def _parameter_role(kind, section, key):
    """Assign each native parameter once; query predicates are operations."""
    if section == "outputParams":
        return "流向目标"
    if section != "inputParams":
        return "处理动作"
    if kind in BRANCHES and key == "condition":
        return "执行条件"
    source_keys = {
        "SetVariable": {"variableValue"}, "SetVariableValue": {"attributeValue"},
        "QueryData": {"paramName"}, "ExitAction": {"returnValue"},
        "ComponentEvent": {"name", "eventName"},
        "SelectData": {"modelName", "tableName", "table", "model", "dataSource", "source"},
    }
    destination_keys = {
        "SetVariableValue": {"variableName", "attributeName"},
        "CallAction": {"actionName"}, "ComponentEvent": {"action"},
    }
    if key in source_keys.get(kind, set()):
        return "数据来源"
    if key in destination_keys.get(kind, set()):
        return "流向目标"
    return "处理动作"


def _role_parameters(node):
    roles = {role: {} for role in ROLES}
    kind = node.get("elementKey")
    roles["处理动作"]["elementKey"] = kind
    params = node.get("paramsValue") or {}
    for section, values in params.items():
        if section in {"inputParams", "outputParams"} and isinstance(values, dict):
            for key, value in values.items():
                path = section + "." + key
                if kind == "SelectData" and path == "inputParams.selectDataConfig" and isinstance(value, dict):
                    for child, configured in value.items():
                        child_path = path + "." + child
                        roles[_path_role(kind, child_path)][child_path] = configured
                else:
                    roles[_parameter_role(kind, section, key)][path] = value
        else:
            roles["处理动作"][section] = values
    return roles


def _path_role(kind, path):
    parts = path.split(".")
    if kind == "SelectData" and parts[:2] == ["inputParams", "selectDataConfig"] and len(parts) > 2:
        key = parts[2].split("[", 1)[0]
        if key in {"outputMapping", "outputMappings", "outputFields", "fieldMapping", "fieldMappings"}:
            return "流向目标"
    return _parameter_role(kind, parts[0], parts[1] if len(parts) > 1 else "")


def _control_nodes(data, target):
    """Follow native depth and Else/ElseIf predecessor links, in source order."""
    group = next((item for item in (data or {}).get("actionData", [])
                  if item.get("key") == target.get("group_key")), {})
    nodes = {item["key"]: item for item in group.get("data", [])}
    node = nodes.get(target.get("node_key"), {})
    result, seen = [], set()

    def add(key):
        if key in seen:
            return
        seen.add(key)
        parent = nodes.get(key)
        if parent and parent.get("elementKey") in {"Else", "ElseIfCondition"}:
            previous = parent.get("depth") or []
            if previous:
                add(previous[-1])
        result.append((key, parent))

    for key in node.get("depth") or []:
        add(key)
    if node.get("elementKey") in BRANCHES:
        add(node["key"])
    return result


def _control_identity(node):
    if node is None:
        return None
    inputs = (node.get("paramsValue") or {}).get("inputParams") or {}
    return [node.get("elementKey"), inputs.get("condition") if node.get("elementKey") in BRANCHES else inputs]


def flow_options(before, after, report, *, display_context=None):
    if display_context is None:
        previous, current = canvas_node_locations(before or {}), canvas_node_locations(after or {})
        impacts = control_flow_impacts(before or {}, after or {}, before_locations=previous, after_locations=current)
    else:
        previous, current = display_context["before_locations"], display_context["after_locations"]
        impacts = display_context["impacts"]
    identifiers = []
    for change in report.get("changes") or []:
        identifier = (change.get("group_key"), change.get("node_key"))
        if identifier in previous or identifier in current:
            identifiers.append(identifier)
    identifiers += [(item["group_key"], item["node_key"]) for item in impacts]
    unique = list(dict.fromkeys(identifiers))
    # The action whose input or execution condition changed is the useful entry.
    unique.sort(key=lambda key: (current.get(key) or previous[key])["node"].get("elementKey") in {"IfCondition", "If", "ElseIfCondition", "Else", "IfEnd"})
    return [{"group_key": key[0], "node_key": key[1],
             "title": (current.get(key) or previous[key])["group"] + " / " +
                      f"第 {(current.get(key) or previous[key])['line']} 行 / " +
                      str((current.get(key) or previous[key])["node"].get("title") or (current.get(key) or previous[key])["node"].get("elementKey"))}
            for key in unique]


def flow_lane(data, target, *, locations=None, triggers_by_group=None):
    group_key, node_key = target.get("group_key"), target.get("node_key")
    locations = canvas_node_locations(data or {}) if locations is None else locations
    side = locations.get((group_key, node_key))
    if not side:
        return {"missing": True, "cards": [], "detail": "此版本不存在该节点"}
    node = side["node"]
    kind = node.get("elementKey")
    params = node.get("paramsValue") or {}
    inputs, outputs = params.get("inputParams") or {}, params.get("outputParams") or {}
    group_nodes = {entry["key"]: entry for group in (data or {}).get("actionData", []) if group.get("key") == group_key for entry in group.get("data", [])}
    source, destination = ("输入 / 参数", "来源未解析"), ("后续数据", "结果以实测为准")
    source_identity, destination_identity = inputs, outputs
    source_field, destination_field = None, None
    if kind == "SetVariable":
        source, destination = parameter(inputs.get("variableValue")), parameter(outputs.get("variableName"))
        source_identity, destination_identity = inputs.get("variableValue"), outputs.get("variableName")
        source_field, destination_field = field_reference(source_identity), field_reference(destination_identity)
    elif kind == "SetVariableValue":
        source, destination = parameter(inputs.get("attributeValue")), parameter(inputs.get("variableName"))
        destination = (destination[0], destination[1] + " · " + str(inputs.get("attributeName") or "属性未配置"))
        source_identity, destination_identity = inputs.get("attributeValue"), [inputs.get("variableName"), inputs.get("attributeName")]
        source_field = field_reference(inputs.get("attributeValue"))
        if inputs.get("attributeName") == "value":
            destination_field = field_reference(inputs.get("variableName"))
    elif kind == "QueryData":
        source, destination = ("页面参数", expression(inputs.get("paramName"))), parameter(outputs.get("parameterResults"))
    elif kind == "CallAction":
        destination = ("调用动作", expression(inputs.get("actionName")))
    elif kind == "ExitAction":
        source, destination = parameter(inputs.get("returnValue")), ("返回结果", "退出当前动作")
    elif kind == "ComponentEvent":
        source = ("组件事件", expression(inputs.get("name")) + " · " + expression(inputs.get("eventName")))
        destination = ("回调动作", expression(inputs.get("action")))
    elif kind == "SelectData":
        model = inputs.get("modelName")
        if isinstance(model, dict):
            source = (str(model.get("modelComment") or model.get("modelName") or "查询表 / 对象"),
                      str(model.get("modelName") or model.get("modelId") or "来源名称未配置"))
        else:
            source = parameter(model)
        destination = parameter(outputs.get("rows")) if "rows" in outputs else ("查询输出", "输出映射未配置")
    conditions = []
    condition_identities = []
    control_target = None
    depth = node.get("depth") or []
    branch = kind in {"ElseIfCondition", "Else"}
    for key in depth[:-1] if branch and depth else depth:
        parent = group_nodes.get(key)
        if parent is None:
            conditions.append("上级关系待解析：" + str(key))
            condition_identities.append([key, None])
            continue
        condition_identities.append([key, parent.get("elementKey"), parent.get("paramsValue")])
        if parent.get("elementKey") in {"If", "IfCondition"}:
            conditions.append(condition_text(((parent.get("paramsValue") or {}).get("inputParams") or {}).get("condition")))
        else:
            conditions.append(operation_rule(parent))
        control_target = {"group_key": group_key, "node_key": key, "field_path": "inputParams.condition"}
    if kind in {"If", "IfCondition", "ElseIfCondition", "Else"}:
        conditions.append(condition_text(inputs.get("condition")) if kind in {"If", "IfCondition"} else operation_rule(node))
        condition_identities.append([node_key, kind, params])
        control_target = {"group_key": group_key, "node_key": node_key, "field_path": "inputParams.condition"}
        destination = ("分支内节点", "条件满足后继续")
    # The native path already handles Else/ElseIf's preceding-branch semantics.
    if any(group_nodes.get(key, {}).get("elementKey") in {"Else", "ElseIfCondition"} for key in depth) or branch:
        conditions = rule_path(data, group_key, node_key, locations=locations,
                               triggers_by_group=triggers_by_group).split("\n→ ")[1:-1] + ([operation_rule(node)] if branch else [])
        # Retain the complete native path for branch-chain identity too.
        condition_identities.append(conditions)
    current_target = {"group_key": group_key, "node_key": node_key}
    operation_text = {"SetVariable": "把来源值赋给目标", "SetVariableValue": "设置目标字段属性", "QueryData": "读取页面参数",
                      "CallAction": "调用后续动作", "ExitAction": "返回结果并结束", "ComponentEvent": "绑定组件事件",
                      "IfCondition": "按条件选择分支", "If": "按条件选择分支", "ElseIfCondition": "检查后续分支", "Else": "执行兜底分支"}.get(kind, str(kind))
    if kind == "SelectData":
        config = inputs.get("selectDataConfig") or {}
        if isinstance(config, dict):
            operation_text = "查询数据"
            if "whereConditions" in config:
                operation_text += "\n筛选：" + condition_text(config["whereConditions"])
            if config.get("sort"):
                operation_text += "\n排序：" + value_text(config["sort"])
    roles = _role_parameters(node)
    source_identity, destination_identity = roles["数据来源"], roles["流向目标"]
    condition_identities = [[key, _control_identity(parent)] for key, parent in _control_nodes(data, target)]
    if "depth" in node:
        condition_identities.append(["depth", node["depth"]])
    condition_title = " 且 ".join("(" + item + ")" for item in conditions) if len(conditions) > 1 else "".join(conditions)
    return {"missing": False, "detail": rule_path(data, group_key, node_key, locations=locations,
                                                   triggers_by_group=triggers_by_group), "cards": [
        {"role": "数据来源", "title": source[0], "body": source[1], "identity": source_identity, "field": source_field, "target": current_target, "changes": []},
        {"role": "执行条件", "title": condition_title or "分组执行到此处", "body": "配置条件 · 命中结果未单独采集", "identity": condition_identities, "target": control_target, "changes": []},
        {"role": "处理动作", "title": str(node.get("title") or kind), "body": operation_text, "identity": roles["处理动作"], "target": current_target, "changes": []},
        {"role": "流向目标", "title": destination[0], "body": destination[1], "identity": destination_identity, "field": destination_field, "target": current_target, "changes": []},
    ]}


def _structural_changes(before, after, path):
    """Use native field/index paths; additions retain the entire new value."""
    if same(before, after):
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in dict.fromkeys([*before, *after]):
            yield from _structural_changes(before.get(key, MISSING), after.get(key, MISSING),
                                           path + "." + str(key) if path else str(key))
    elif isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            yield from _structural_changes(before[index] if index < len(before) else MISSING,
                                           after[index] if index < len(after) else MISSING,
                                           f"{path}[{index}]")
    else:
        yield path, before, after


_FIELD_TITLES = {
    "elementKey": "元件类型", "title": "元件名称", "description": "元件说明", "depth": "上级控制关联",
    "inputParams": "输入参数", "outputParams": "输出参数", "modelName": "来源表 / 对象",
    "selectDataConfig": "查询配置", "whereConditions": "筛选条件", "condition": "判断条件",
    "Logic": "条件组合方式", "Filters": "条件", "Field": "筛选字段", "Operator": "比较方式",
    "target": "判断对象", "equalTo": "比较方式", "ParamInput": "条件取值", "sort": "排序",
    "rows": "查询结果变量", "variableValue": "来源值", "attributeValue": "来源值",
    "variableName": "目标变量", "attributeName": "目标属性", "parameterResults": "参数结果变量",
    "paramName": "页面参数名", "returnValue": "返回值", "actionName": "调用动作", "action": "回调动作",
    "label": "显示名称", "code": "完整表达式", "value": "配置值", "paramTypes": "取值方式",
    "dataType": "数据类型", "type": "字段类型", "modelComment": "来源名称", "modelId": "来源标识",
}


def _field_title(path, before, after):
    pieces = []
    for part in path.split("."):
        match = re.fullmatch(r"([^\[]+)(\[\d+\])?", part)
        key, index = match.groups() if match else (part, None)
        if key in {"inputParams", "outputParams", "selectDataConfig"}:
            continue
        title = _FIELD_TITLES.get(key, key)
        if index:
            title += " " + str(int(index[1:-1]) + 1)
        pieces.append(title)
    value = after if after is not MISSING else before
    if isinstance(value, dict) and "Field" in value:
        pieces.append(str(value["Field"]))
    return " / ".join(pieces or ["配置"])


def _configuration_text(value):
    if value is MISSING:
        return "未配置此项"
    if isinstance(value, dict) and "Field" in value and "Operator" in value:
        text = condition_text(value)
        parameter_value = value.get("ParamInput")
        if isinstance(parameter_value, dict) and parameter_value.get("label"):
            text += "（" + str(parameter_value["label"]) + "）"
        extra = {key: item for key, item in value.items()
                 if key not in {"Field", "Operator", "value", "type", "ParamInput"}}
        if extra:
            text += "\n其它配置：" + json.dumps(extra, ensure_ascii=False, indent=2)
        return text
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return value_text(value)


def _change_rows(before_node, after_node, target, *, control=False):
    before_node, after_node = before_node or {}, after_node or {}
    before_params, after_params = before_node.get("paramsValue") or {}, after_node.get("paramsValue") or {}
    rows = []

    def add(role, path, previous, current):
        location = {**target, "field_path": path}
        field = _field_title(path, previous, current)
        if control:
            field = "上级控制 · " + field
        previous_text, current_text = _configuration_text(previous), _configuration_text(current)
        if path == "description":
            if isinstance(previous, str):
                previous_text = plain_description({"description": previous}) or '空字符串 ""'
            if isinstance(current, str):
                current_text = plain_description({"description": current}) or '空字符串 ""'
            if previous is not MISSING and current is not MISSING and previous_text == current_text:
                field += "（仅说明格式变化）"
        rows.append({"role": role, "field": field, "path": path,
                     "before": previous_text, "after": current_text,
                     "before_value": previous, "after_value": current,
                     "kind": "新增" if previous is MISSING else "删除" if current is MISSING else "修改",
                     "target": location})

    # Parameters precede generated captions, so concrete edits are immediately visible.
    for section in dict.fromkeys([*before_params, *after_params]):
        old_values, new_values = before_params.get(section, {}), after_params.get(section, {})
        if section in {"inputParams", "outputParams"} and isinstance(old_values, dict) and isinstance(new_values, dict):
            for key in dict.fromkeys([*old_values, *new_values]):
                kind = (after_node if key in new_values else before_node).get("elementKey")
                if control and (section != "inputParams" or kind in BRANCHES and key != "condition"):
                    continue
                for path, previous, current in _structural_changes(old_values.get(key, MISSING), new_values.get(key, MISSING), section + "." + key):
                    add("执行条件" if control else _path_role(kind, path), path, previous, current)
        elif not control:
            for path, previous, current in _structural_changes(before_params.get(section, MISSING), after_params.get(section, MISSING), section):
                add("处理动作", path, previous, current)
    for key in ("elementKey", "depth") if control else ("elementKey", "depth", "title", "description"):
        for path, previous, current in _structural_changes(before_node.get(key, MISSING), after_node.get(key, MISSING), key):
            add("执行条件" if control or key == "depth" else "处理动作", path, previous, current)
    return rows


def flow_comparison(before, after, target, *, display_context=None):
    """Complete before/after rows for this node and the controls it depends on.

    This is a display model, independent of approval reports and runtime checks.
    Raw configuration values are retained alongside their readable text.
    """
    if display_context is None:
        previous, current = canvas_node_locations(before or {}), canvas_node_locations(after or {})
        previous_triggers = current_triggers = None
    else:
        previous, current = display_context["before_locations"], display_context["after_locations"]
        previous_triggers, current_triggers = display_context["before_triggers"], display_context["after_triggers"]
    baseline = flow_lane(before, target, locations=previous, triggers_by_group=previous_triggers)
    candidate = flow_lane(after, target, locations=current, triggers_by_group=current_triggers)
    identity = target.get("group_key"), target.get("node_key")
    old_node, new_node = (previous.get(identity) or {}).get("node"), (current.get(identity) or {}).get("node")
    location = {"group_key": identity[0], "node_key": identity[1]}
    changes = _change_rows(old_node, new_node, location)
    old_controls, new_controls = dict(_control_nodes(before, target)), dict(_control_nodes(after, target))
    for key in dict.fromkeys([*old_controls, *new_controls]):
        if key == identity[1]:
            continue
        changes.extend(_change_rows(old_controls.get(key), new_controls.get(key),
                                    {"group_key": identity[0], "node_key": key}, control=True))
    for lane in (baseline, candidate):
        for card in lane["cards"]:
            card["changes"] = [row for row in changes if row["role"] == card["role"]]
    return {"baseline": baseline, "candidate": candidate, "changes": changes}


def field_names(before, after, *, display_context=None):
    result = {}
    for version, data in (("before", before), ("after", after)):
        locations = canvas_node_locations(data or {}) if display_context is None else display_context[version + "_locations"]
        for side in locations.values():
            params = side["node"].get("paramsValue") or {}
            inputs, outputs = params.get("inputParams") or {}, params.get("outputParams") or {}
            values = list(inputs.values()) + list(outputs.values())
            for value in values:
                if not isinstance(value, dict):
                    continue
                identifier = field_reference(value)
                if identifier and value.get("label"):
                    result[identifier] = str(value["label"])
    return result


def _field_value(evidence, identifier, attribute="value"):
    actual = evidence.get("actual")
    if not isinstance(actual, dict):
        return MISSING
    presence = (actual.get("field_value_presence") or {}).get(identifier)
    if attribute == "value" and presence == "undefined":
        return UNDEFINED
    return (actual.get("fields", {}).get(identifier) or {}).get(attribute, MISSING)


def field_states(case, spec, names=None):
    names = names or {}
    environment = spec.get("environment") or {}
    expected = case.get("expected", spec.get("expect", {}))
    expected_fields = expected.get("fields", {}) if isinstance(expected, dict) else {}
    baseline, candidate = case.get("baseline") or {}, case.get("candidate") or {}
    initial_fields = environment.get("fields") or {}
    identifiers = list(initial_fields) + list(expected_fields)
    for evidence in (baseline, candidate):
        actual = evidence.get("actual")
        if isinstance(actual, dict):
            identifiers += list(actual.get("fields") or {})
    rows = []
    properties = []
    for identifier in dict.fromkeys(identifiers):
        attributes = set(initial_fields.get(identifier, {})) | set(expected_fields.get(identifier, {}))
        for evidence in (baseline, candidate):
            actual = evidence.get("actual")
            if isinstance(actual, dict):
                attributes |= set((actual.get("fields") or {}).get(identifier) or {})
        properties.extend((identifier, attribute) for attribute in ("value", "text", "selected_rows") if attribute in attributes)
        if not attributes:
            properties.append((identifier, "value"))
    for identifier, attribute in properties:
        initial = initial_fields.get(identifier, {}).get(attribute, MISSING)
        expectation = expected_fields.get(identifier, {}).get(attribute, MISSING)
        sides = []
        for evidence in (baseline, candidate):
            trace = evidence.get("trace") or []
            writes = [entry for entry in trace if attribute == "value" and entry.get("api") == identifier + ".value.write"]
            read_api = {"value": ".value.read", "text": ".getText", "selected_rows": ".getSelectedRows"}[attribute]
            reads = [entry for entry in trace if entry.get("api") == identifier + read_api]
            observed = _field_value(evidence, identifier, attribute)
            input_changes = any(identifier in (step.get("fields") or {}) for step in spec.get("steps") or [] if step.get("kind") == "set_inputs")
            if observed is MISSING:
                behavior = "结果未观测"
            elif input_changes:
                behavior = "含外部输入变化"
            elif writes:
                behavior = "同值写入" if same(initial, observed) else "覆盖原值" if initial is not MISSING and initial is not None else "写入新值"
            elif isinstance(evidence.get("trace"), list) and evidence.get("status") in {"passed", "failed"} and same(initial, observed):
                behavior = "保留原值"
            else:
                behavior = "无完整读写证据"
            verdict = "unverified"
            if expectation is not MISSING and observed is not MISSING and evidence.get("status") in {"passed", "failed"}:
                assertions = [entry for entry in evidence.get("assertions") or [] if entry.get("name") == f"fields.{identifier}.{attribute}"]
                if not same(expectation, observed):
                    verdict = "failed"
                elif evidence.get("status") == "passed" or any(entry.get("passed") is True for entry in assertions):
                    verdict = "passed"
            sides.append({"value": observed, "behavior": behavior, "writes": len(writes), "reads": len(reads),
                          "verdict": verdict, "execution_status": evidence.get("status", "unverified")})
        suffix = {"value": "", "text": " · 显示文本", "selected_rows": " · 选中行"}[attribute]
        rows.append({"id": identifier, "attribute": attribute, "name": names.get(identifier, identifier) + suffix, "initial": initial, "expected": expectation,
                     "asserted": expectation is not MISSING, "baseline": sides[0], "candidate": sides[1]})
    rows.sort(key=lambda row: (not row["asserted"], not (row["baseline"]["writes"] or row["candidate"]["writes"])))
    return rows


def trace_events(evidence, names=None):
    """Arrows in the UI express observed order, never inferred data dependency."""
    names = names or {}
    rows = []
    for index, entry in enumerate(evidence.get("trace") or []):
        api = str(entry.get("api") or "未知调用")
        role, title, body = "调用", api, ""
        for suffix, label in ((".value.read", "读取"), (".value.write", "写入"), (".getSelectedRows", "读取选中行"), (".getText", "读取文本")):
            if api.endswith(suffix):
                identifier = api[:-len(suffix)]
                role, title, body = label, names.get(identifier, identifier), value_text(entry.get("value", MISSING))
                break
        else:
            if api.startswith("group."):
                role, title = "执行分组", api[6:]
                body = "步骤 " + str(entry.get("step", 0) + 1)
            elif api == "inbiz.getPageMode":
                role, title, body = "读取", "页面模式", str(entry.get("value", "未观测"))
            elif api.startswith("event."):
                role, title = "触发事件", api[6:]
            elif api.startswith("metadata."):
                role, title, body = "元数据", api, "版本 " + str(entry.get("version", "未知"))
            elif entry.get("event"):
                body = str(entry["event"])
        rows.append({"role": role, "title": title, "body": body, "detail": deepcopy(entry), "index": index})
    return rows


def output_states(case, spec):
    """Other observed payloads, without implying SDK requests or backend writes."""
    expected = case.get("expected", spec.get("expect", {}))
    expected = expected if isinstance(expected, dict) else {}
    rows = []
    for key, title in (("metadata", "页面元数据"), ("metadata_registrations", "元数据注册次数"), ("last_return", "动作返回数据")):
        expectation = UNDEFINED if key == "last_return" and expected.get("last_return_undefined") is True else expected.get(key, MISSING)
        sides = []
        for version in ("baseline", "candidate"):
            evidence = case.get(version) or {}
            actual = evidence.get("actual")
            actual = actual if isinstance(actual, dict) else {}
            value = UNDEFINED if key == "last_return" and actual.get("last_return_undefined") is True else actual.get(key, MISSING)
            verdict = "unverified"
            assertion_name = "last_return_undefined" if key == "last_return" and expectation is UNDEFINED else key
            checked = any(item.get("name") == assertion_name and item.get("passed") is True for item in evidence.get("assertions") or [])
            if expectation is not MISSING and value is not MISSING and evidence.get("status") in {"passed", "failed"}:
                if not same(value, expectation):
                    verdict = "failed"
                elif checked or evidence.get("status") == "passed":
                    verdict = "passed"
            sides.append({"value": value, "verdict": verdict})
        observed = any(side["value"] is not MISSING and side["value"] is not UNDEFINED and side["value"] is not None
                       and (key != "metadata_registrations" or side["value"] != 0) for side in sides)
        if expectation is not MISSING or observed:
            rows.append({"key": key, "title": title, "expected": expectation, "baseline": sides[0], "candidate": sides[1]})
    return rows
