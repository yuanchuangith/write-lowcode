"""Display-only native operation descriptions; never evaluate expressions."""
from __future__ import annotations

from .approval_display import _condition, _parameter_summary
from .component_preview import canvas_node_locations, control_flow_impacts, plain_description


def operation_rule(node):
    if node is None:
        return "此侧不存在该元件"
    kind = node.get("elementKey")
    params = node.get("paramsValue") or {}
    inputs, outputs = params.get("inputParams") or {}, params.get("outputParams") or {}
    value = _parameter_summary
    if kind in {"If", "IfCondition", "ElseIfCondition"}:
        return "判断条件：" + _condition(inputs.get("condition") or {})
    if kind == "Else":
        return "此前同链 IF / ElseIf 条件均未命中时执行此分支"
    if kind == "SetVariable":
        return f"取值：{value(inputs.get('variableValue'))} → 赋值给：{value(outputs.get('variableName'))}"
    if kind == "SetVariableValue":
        attribute = inputs.get("attributeName")
        target = value(inputs.get("variableName")) + (" 的属性 " + str(attribute) if attribute else "")
        return f"取值：{value(inputs.get('attributeValue'))} → 设置：{target}"
    if kind == "QueryData":
        return f"读取页面参数：{value(inputs.get('paramName'))} → 保存至：{value(outputs.get('parameterResults'))}"
    if kind == "CallAction":
        return "调用子动作：" + value(inputs.get("actionName"))
    if kind == "ComponentEvent":
        return f"注册组件事件：{value(inputs.get('name'))} / {value(inputs.get('eventName'))} → 回调：{value(inputs.get('action'))}（注册时机待场景验证）"
    if kind == "ExitAction":
        return "退出当前动作，返回配置值：" + value(inputs.get("returnValue")) + "（返回类型以生成代码及场景证据为准）"
    return (plain_description(node) or str(kind)) + "；原始配置见元件详情，表达式语义待解析"


def rule_path(data, group_key, node_key, *, locations=None, triggers_by_group=None):
    locations = canvas_node_locations(data or {}) if locations is None else locations
    current = locations.get((group_key, node_key))
    if current is None:
        return "此侧不存在该元件"
    group = next(item for item in data["actionData"] if item["key"] == group_key)
    nodes = {node["key"]: node for node in group.get("data", [])}
    triggers_by_group = event_triggers(data) if triggers_by_group is None else triggers_by_group
    triggers = triggers_by_group.get(group_key, [])
    path = ["触发：" + ("；".join(triggers) if triggers else f"执行分组“{current['group']}”（入口时机待验证）")]
    def prior_branches(branch):
        prior = []
        seen = set()
        while branch.get("elementKey") in {"ElseIfCondition", "Else"} and branch["key"] not in seen:
            seen.add(branch["key"])
            depth = branch.get("depth") or []
            previous = nodes.get(depth[-1]) if depth else None
            if previous is None or previous.get("elementKey") not in {"If", "IfCondition", "ElseIfCondition"}:
                prior.append("前置分支关联待解析")
                break
            prior.append(operation_rule(previous))
            branch = previous
        return prior

    current_node = current["node"]
    depth = current_node.get("depth") or []
    branch_node = current_node.get("elementKey") in {"ElseIfCondition", "Else"}
    # A branch header's final depth item names its preceding branch, not
    # an enclosing condition that must be true.
    for key in depth[:-1] if branch_node and depth else depth:
        parent = nodes.get(key)
        if not parent:
            path.append("上级控制配置未解析：" + str(key))
            continue
        prior = prior_branches(parent)
        if prior:
            path.append("未命中前置分支：" + "；".join(reversed(prior)))
        path.append(operation_rule(parent))
    prior = prior_branches(current_node)
    if prior:
        path.append("未命中前置分支：" + "；".join(reversed(prior)))
    path.append(operation_rule(current_node))
    return "\n→ ".join(path)


def event_triggers(data):
    triggers = {}
    for owner in data.get("actionData", []):
        for node in owner.get("data", []):
            inputs = (node.get("paramsValue") or {}).get("inputParams") or {}
            target = inputs.get("action") or {}
            if node.get("elementKey") == "ComponentEvent" and isinstance(target, dict) and target.get("value"):
                triggers.setdefault(target["value"], []).append(f"组件 {_parameter_summary(inputs.get('name'))} 的 {_parameter_summary(inputs.get('eventName'))} 事件（绑定需先执行）")
    return triggers


def review_display_context(before, after):
    controlled = {}
    for item in control_flow_impacts(before or {}, after or {}):
        for parent in item["affected_by"]:
            controlled.setdefault((parent["group_key"], parent["node_key"]), []).append(item)
    return {"before_locations": canvas_node_locations(before or {}), "after_locations": canvas_node_locations(after or {}),
            "before_triggers": event_triggers(before or {}), "after_triggers": event_triggers(after or {}),
            "controlled": controlled, "rule_cache": {}}


def change_display_context(change, before, after, *, context=None):
    context = review_display_context(before, after) if context is None else context
    group_key, node_key = change.get("group_key"), change.get("node_key")
    previous, current = context["before_locations"], context["after_locations"]
    side = current.get((group_key, node_key)) or previous.get((group_key, node_key))
    location = (f"{side['group']} · 第 {side['line']} 行 · {side['node'].get('title') or side['node']['elementKey']}"
                if side else str(group_key or "画布") + (" · 分组配置" if not node_key else " · " + str(node_key)))
    field = change.get("field_path") or ""
    impact = change.get("impact") or "影响待核对"
    controlled = []
    for item in context["controlled"].get((group_key, node_key), []):
        affected = item["after"] or item["before"]
        controlled.append(f"{affected['group']} 第 {affected['line']} 行 {affected['node'].get('title') or affected['node']['elementKey']}")
    if controlled:
        impact += "\n控制流关联：" + "；".join(controlled) + "（是否执行仍需场景验证）"
    def rule(version, data):
        cache_key = version, group_key, node_key
        if cache_key not in context["rule_cache"]:
            context["rule_cache"][cache_key] = rule_path(data or {}, group_key, node_key,
                locations=context[version + "_locations"], triggers_by_group=context[version + "_triggers"])
        return context["rule_cache"][cache_key]
    return {"location": location + ("\n配置字段：" + field if field else ""),
            "before_rule": rule("before", before) if side else change.get("before_rule", change.get("before")),
            "after_rule": rule("after", after) if side else change.get("after_rule", change.get("after")),
            "impact": impact, "field_before": change.get("before"), "field_after": change.get("after")}
