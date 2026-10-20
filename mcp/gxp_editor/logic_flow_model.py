"""Display models for configuration flow and observed field state; no evaluation."""
from __future__ import annotations

import json
import re
from copy import deepcopy

from .component_preview import canvas_node_locations, control_flow_impacts
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
        return str(value.get("code") if value.get("code") is not None else value.get("value") or "")
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
        return joiner.join("(" + condition_text(item) + ")" if "Filters" in item else condition_text(item)
                           for item in condition["Filters"]) or "未配置条件"
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
        return code + " " + ops.get(op, op) + ("" if op in {"EqualNull", "NotEqualNull"} else " " + expression(expected))
    return expression(condition)


def flow_options(before, after, report):
    previous, current = canvas_node_locations(before or {}), canvas_node_locations(after or {})
    identifiers = []
    for change in report.get("changes") or []:
        identifier = (change.get("group_key"), change.get("node_key"))
        if identifier in previous or identifier in current:
            identifiers.append(identifier)
    identifiers += [(item["group_key"], item["node_key"]) for item in control_flow_impacts(before or {}, after or {})]
    unique = list(dict.fromkeys(identifiers))
    # The action whose input or execution condition changed is the useful entry.
    unique.sort(key=lambda key: (current.get(key) or previous[key])["node"].get("elementKey") in {"IfCondition", "If", "ElseIfCondition", "Else", "IfEnd"})
    return [{"group_key": key[0], "node_key": key[1],
             "title": (current.get(key) or previous[key])["group"] + " / " +
                      str((current.get(key) or previous[key])["node"].get("title") or (current.get(key) or previous[key])["node"].get("elementKey"))}
            for key in unique]


def flow_lane(data, target):
    group_key, node_key = target.get("group_key"), target.get("node_key")
    locations = canvas_node_locations(data or {})
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
        conditions = rule_path(data, group_key, node_key).split("\n→ ")[1:-1] + ([operation_rule(node)] if branch else [])
        # Retain the complete native path for branch-chain identity too.
        condition_identities.append(conditions)
    current_target = {"group_key": group_key, "node_key": node_key}
    operation_text = {"SetVariable": "把来源值赋给目标", "SetVariableValue": "设置目标字段属性", "QueryData": "读取页面参数",
                      "CallAction": "调用后续动作", "ExitAction": "返回结果并结束", "ComponentEvent": "绑定组件事件",
                      "IfCondition": "按条件选择分支", "If": "按条件选择分支", "ElseIfCondition": "检查后续分支", "Else": "执行兜底分支"}.get(kind, str(kind))
    return {"missing": False, "detail": rule_path(data, group_key, node_key), "cards": [
        {"role": "数据来源", "title": source[0], "body": source[1], "identity": source_identity, "field": source_field, "target": current_target},
        {"role": "执行条件", "title": " 且 ".join(conditions) or "分组执行到此处", "body": "配置条件 · 命中结果未单独采集", "identity": condition_identities, "target": control_target},
        {"role": "处理动作", "title": str(node.get("title") or kind), "body": operation_text, "identity": [kind, params], "target": current_target},
        {"role": "流向目标", "title": destination[0], "body": destination[1], "identity": destination_identity, "field": destination_field, "target": current_target},
    ]}


def field_names(before, after):
    result = {}
    for data in (before, after):
        for side in canvas_node_locations(data or {}).values():
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
