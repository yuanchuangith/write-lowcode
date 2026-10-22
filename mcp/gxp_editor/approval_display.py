"""Read-only presentation helpers for the local approval application."""
from __future__ import annotations

import json
import re
from typing import Any

from pymysql.converters import escape_item
from .action_context import publication_notice
from .single_row_checker import check_single_row_statement
from .component_preview import component_changes, plain_description


STATEMENT_LABELS = {
    "update_design": "保存一个未发布草稿",
    "delete_relation": "删除一条动作关联记录",
    "update_relation": "更新一条动作关联记录",
    "restore_relation": "恢复一条动作关联记录",
    "insert_relation": "新增一条动作关联记录",
}


def render_full_sql(statement: dict[str, Any]) -> str:
    """Expand a display copy; execution still uses the signed template and params.

    Format the template before binding, so SQL-like text inside JSON/code is never
    reformatted. PyMySQL supplies MySQL string escaping; no connection is opened.
    """
    sql = statement["sql"]
    params = statement["params"]
    if sql.count("%s") != len(params):
        raise ValueError("SQL 占位符数量与参数数量不一致")
    template = (
        sql.replace(" SET ", "\nSET ")
        .replace(" WHERE ", "\nWHERE ")
        .replace(" AND ", "\n  AND ")
        .replace(" VALUES ", "\nVALUES ")
        .replace(", ", ",\n    ")
    )
    return (template % tuple(escape_item(value, "utf8mb4") for value in params)) + ";"


def statement_scope(statement: dict[str, Any]) -> str:
    """Expose the actual bound row guards without the large SET payloads."""
    kind = statement["kind"]
    params = statement["params"]
    check = check_single_row_statement(statement)
    lines = [check["summary"], f"操作：{STATEMENT_LABELS.get(kind, kind)}", f"预计影响行数：{statement['expected_rows']} 行（不符则回滚）"]
    if kind == "update_design":
        lines.extend([
            f"草稿 ID：{params[4]}",
            "限定：is_publish = 0（未发布），isDeleted = 0（未删除）",
            f"原修改时间：{params[5]}（有并发修改则停止）",
            "更新字段：data、js_code、csharp_code、lastModificationTime",
        ])
    elif kind == "insert_relation":
        lines.extend([f"关联记录 ID：{params[0]}", f"所属草稿 ID：{params[10]}"])
    else:
        lines.extend([f"关联记录 ID：{params[-2]}", f"所属草稿 ID：{params[-1]}"])
    return "\n".join(lines)


def _label(value: Any) -> str:
    if isinstance(value, dict):
        selected = next((value[key] for key in ("label", "code", "value") if key in value and value[key] is not None and value[key] != ""), None)
        return _label(selected).removeprefix("页面组件-").removeprefix("系统变量-").removeprefix("局部变量-").removesuffix("-当前值")
    if value is None:
        return "未设置"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _parameter_summary(value: Any) -> str:
    """Keep picker labels readable without hiding the executable identity."""
    if not isinstance(value, dict):
        return _label(value)
    name = _label(value)
    code = value.get("code")
    kind = str(value.get("paramTypes") or "")
    kinds = {"custom": "自定义表达式", "localVariable": "局部变量", "inputParam": "本分组输入参数",
             "outputParam": "本分组输出参数", "globalVariable": "全局变量", "componentsVariable": "页面组件"}
    details = []
    if kind:
        details.append(kinds.get(kind, kind))
    if code is not None and str(code) != name:
        details.append("表达式：" + str(code))
    configured = value.get("value")
    if configured is not None and configured != code and str(configured) != kind + "-" + str(code):
        details.append("配置值：" + str(configured))
    if value.get("objectAttribute"):
        details.append("属性：" + str(value["objectAttribute"]))
    return name + ("（" + "；".join(details) + "）" if details else "")


def _condition(condition: dict[str, Any]) -> str:
    if not isinstance(condition, dict):
        return _parameter_summary(condition)
    if "Filters" not in condition and any(key in condition for key in ("target", "Field")):
        condition = {"Filters": [condition]}
    if "paramTypes" in condition:
        return _parameter_summary(condition)
    operators = {"Equal": "等于", "NotEqual": "不等于", "EqualNull": "为空", "NotEqualNull": "不为空",
                 "GreaterThan": "大于", "LessThan": "小于", "GreaterThanOrEqual": "大于等于", "LessThanOrEqual": "小于等于"}
    parts = []
    for item in condition.get("Filters", []):
        if "Filters" in item:
            parts.append("（" + _condition(item) + "）")
        else:
            op = str(item.get("equalTo") or item.get("Operator") or "")
            phrase = f"{_parameter_summary(item.get('target') or item.get('Field'))} {operators.get(op, op)}"
            if op not in {"EqualNull", "NotEqualNull"}:
                phrase += " " + _parameter_summary(item.get("value"))
            parts.append(phrase)
    return ("，并且 " if str(condition.get("Logic", "And")).lower() == "and" else "，或者 ").join(parts) or "未设置条件"


def canvas_change_summary(before: dict[str, Any], after: dict[str, Any], *, changes=None) -> str:
    """Summarize real parameter/position changes, excluding generated metadata."""
    def source_context(data, side):
        # A source belongs to one version, group and preceding lexical scope.
        # Do not attribute a same-named output from a later/other branch.
        queries, new_flags, declarations = {}, set(), {}
        if not side:
            return queries, new_flags
        for group in data.get("actionData", []):
            if group["key"] != side.get("group_key"):
                continue
            consumer_depth = side["node"].get("depth") or []
            for node in group.get("data", []):
                if node["key"] == side["node"]["key"]:
                    break
                depth = node.get("depth") or []
                if depth and depth[-1] not in consumer_depth:
                    continue
                params = node.get("paramsValue", {})
                inputs, outputs = params.get("inputParams", {}), params.get("outputParams", {})
                for output in outputs.values():
                    if isinstance(output, dict) and output.get("code"):
                        name = str(output["code"])
                        declarations[name] = declarations.get(name, 0) + 1
                if node.get("elementKey") == "QueryData":
                    name = outputs.get("parameterResults", {}).get("code")
                    if name:
                        queries[name] = _label(inputs.get("paramName"))
                variable_value = inputs.get("variableValue")
                if isinstance(variable_value, dict) and variable_value.get("code") == '(inbiz.getPageMode()=="add")':
                    new_flags.add(str(outputs.get("variableName", {}).get("code")))
        # Review must not resolve ambiguous legacy declarations by dict order.
        return ({name: source for name, source in queries.items() if declarations.get(name) == 1},
                {name for name in new_flags if declarations.get(name) == 1})

    def source(value: Any, target: str, context, output_code: str) -> str:
        queries, new_flags = context
        if isinstance(value, dict):
            code = str(value.get("code") or "")
            if code in queries:
                return f"直接使用页面参数“{queries[code]}”覆盖“{target}”"
            match = re.fullmatch(r"\((\w+) && !relatedAttributes\.isEmpty\((\w+)\)\)\s*\?\s*(\w+)\s*:\s*(.+)", code)
            if match and match[1] in new_flags and match[2] == match[3] and match[2] in queries:
                if match[4] == output_code:
                    return f"仅在新增申请、且页面参数“{queries[match[2]]}”非空时赋值；重新打开已有申请时保留“{target}”原值"
            if value.get("paramTypes") == "custom" and len(code) > 100:
                return "使用新的自定义计算规则（完整表达式见“原始节点详情”）"
        return _parameter_summary(value)

    chunks: list[str] = []
    items = component_changes(before, after) if changes is None else changes
    for count, item in enumerate(items, 1):
        old_side, new_side = item["before"], item["after"]
        side = new_side or old_side
        node = side["node"]
        old = old_side["node"] if old_side else None
        location = f"{side['group']} → 第 {side['line']} 行 → {node.get('title') or node['elementKey']}"
        if node["elementKey"] == "actionGroup":
            def group_text(value):
                return json.dumps(value["node"]["paramsValue"]["inputParams"], ensure_ascii=False, indent=2) if value else "不存在"
            chunks.append(f"{count}. {item['kind']}动作分组：{side['group']}\n修改前：{group_text(old_side)}\n修改后：{group_text(new_side)}")
            continue
        if node["elementKey"] == "globalVariables":
            chunks.append(f"{count}. 全局变量发生变更，完整内容见“元件预览”。")
            continue
        if old_side is None:
            chunks.append(f"{count}. 新增节点\n位置：{location}\n内容：{plain_description(node) or '新增一项操作'}")
            continue
        if new_side is None:
            chunks.append(f"{count}. 删除节点\n位置：{location}")
            continue
        structural = []
        if item["kind"] in {"移动", "修改并移动"}:
            structural.append(f"位置变化：第 {old_side['line']} 行 → 第 {new_side['line']} 行")
        if (old.get("depth") or []) != (node.get("depth") or []):
            layer = lambda value: " → ".join(value.get("parents") or []) or "分组顶层"
            structural.append(f"所在层级\n修改前：{layer(old_side)}\n修改后：{layer(new_side)}")
        if old.get("elementKey") != node.get("elementKey"):
            structural.append(f"元件类型：{old['elementKey']} → {node['elementKey']}")
        if old.get("paramsValue") == node.get("paramsValue"):
            detail = "\n".join(structural)
        else:
            inputs = node.get("paramsValue", {}).get("inputParams", {})
            old_inputs = old.get("paramsValue", {}).get("inputParams", {})
            if node["elementKey"] in {"SetVariable", "SetVariableValue"} and "variableValue" in inputs:
                target_value = node.get("paramsValue", {}).get("outputParams", {}).get("variableName", {})
                target = _label(target_value)
                old_target_value = old.get("paramsValue", {}).get("outputParams", {}).get("variableName", {})
                old_target = _label(old_target_value)
                old_rule = source(old_inputs.get('variableValue'), old_target, source_context(before, old_side), str(old_target_value.get("code") or ""))
                new_rule = source(inputs.get('variableValue'), target, source_context(after, new_side), str(target_value.get("code") or ""))
                detail = f"修改字段：{target}\n修改前：{old_rule}\n修改后：{new_rule}"
                if old.get("paramsValue", {}).get("outputParams") != node.get("paramsValue", {}).get("outputParams"):
                    old_target = _label(old.get("paramsValue", {}).get("outputParams", {}).get("variableName"))
                    detail += f"\n赋值目标：{old_target} → {target}"
                if old_inputs.get("variableType") != inputs.get("variableType"):
                    detail += f"\n数据类型：{_label(old_inputs.get('variableType'))} → {_label(inputs.get('variableType'))}"
            elif "condition" in inputs:
                detail = f"修改字段：执行条件\n修改前：{_condition(old_inputs.get('condition', {}))}\n修改后：{_condition(inputs['condition'])}"
            else:
                changes = []
                for section in ("inputParams", "outputParams"):
                    previous = old.get("paramsValue", {}).get(section, {})
                    current = node.get("paramsValue", {}).get(section, {})
                    for field in sorted(set(previous) | set(current)):
                        if previous.get(field) != current.get(field):
                            changes.append(f"字段“{field}”\n修改前：{_parameter_summary(previous.get(field))}\n修改后：{_parameter_summary(current.get(field))}")
                detail = "\n".join(changes) or "节点类型发生变更，完整配置见“原始节点详情”。"
            if node["elementKey"] == "SetVariable":
                previous, following = new_side["previous"], new_side["next"]
                name = lambda adjacent: adjacent["name"] if adjacent else "无（分组边界）"
                detail += (
                    f"\n草稿查看：打开该动作草稿 → {location} → 元件配置 → 输入参数 → 变量值"
                    f"\n相邻元件：上一行“{name(previous)}”；下一行“{name(following)}”"
                )
                value = inputs.get("variableValue")
                if isinstance(value, dict) and value.get("paramTypes") == "custom":
                    detail += f"\n配置方式：自定义表达式（在原元件内，未新增 IF 元件）\n变量值（完整）：{value.get('value', '未设置')}"
                    if value.get("value") != value.get("code"):
                        detail += "\n警告：显示值与生成表达式不一致，重新保存可能覆盖修改。"
            if structural:
                detail += "\n" + "\n".join(structural)
        heading = "移动节点" if item["kind"] == "移动" else "修改节点"
        chunks.append(f"{count}. {heading}\n位置：{location}\n{detail}")
    count = len(items)
    return f"本次共 {count} 项画布变更\n\n" + "\n\n".join(chunks) if count else "画布参数未变更。"


def parameter_details(statement: dict[str, Any]) -> str:
    chunks = ["原始参数化 SQL", statement["sql"], "\n完整参数（未截断）"]
    for index, value in enumerate(statement["params"], 1):
        chunks.extend([f"\n参数 {index}", value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)])
    return "\n".join(chunks)


def execution_result_message(result: dict[str, Any]) -> str:
    if result.get("state") == "saved":
        if not (result.get("verified") and result.get("session_released")):
            return ("草稿已保存，核验收尾尚未完成。\n\n"
                    "请回到当前对话核对结果，完成前请勿在平台再次保存或手动发布。\n"
                    "当前运行仍使用发布副本，此次保存尚未生效。\n\n"
                    + str(result.get("verification_error") or "等待编辑工具核对草稿并结束会话。"))
        return (
            "草稿已保存成功，已核验并结束编辑会话。\n\n"
            "当前运行仍使用发布副本，此次保存尚未生效。\n"
            + (publication_notice(result.get("publication_targets"))
               or "请按本次审批中的动作名称和编号，在平台检查草稿并手动发布。")
            + "\n请检查画布后逐一发布，再重新打开业务页面验证。"
        )
    return "事务结果：\n" + json.dumps(result, ensure_ascii=False, indent=2, default=str)


def reference_repair_summary(repairs: list[dict[str, Any]]) -> str:
    chunks = [f"本次修复 {len(repairs)} 处旧参数引用。整份编排已通过编译校验，仍需逐条审批后才能保存。"]
    for index, repair in enumerate(repairs, 1):
        source = repair["source"]
        kind = {"inputParam": "本分组输入参数", "outputParam": "本分组输出参数",
                "localVariable": "当前作用域局部变量", "globalVariable": "全局变量"}.get(source["kind"], source["kind"])
        origin = source.get("node_key") or source.get("parameter_key") or source.get("name")
        path = ".".join(str(part) for part in repair["path"])
        chunks.append(f"{index}. 分组 {repair['group_key']} · 第 {repair['row']} 行 · 节点 {repair['node_key']}\n"
                      f"配置字段：{path}\n来源：{kind} · {origin}\n"
                      f"选择方式：{'唯一匹配' if repair['selection'] == 'automatic' else '已确认歧义'}\n"
                      f"修改前：{json.dumps(repair['before'], ensure_ascii=False)}\n"
                      f"修改后：{json.dumps(repair['after'], ensure_ascii=False)}")
    return "\n\n".join(chunks)


def generation_review_summary(evidence: dict[str, Any]) -> str:
    generation = evidence.get("generation") or {}
    selection = evidence.get("generation_selection") or {}
    labels = {"full": "全量生成", "incremental": "增量生成", "auto": "自动选择"}
    reasons = {"explicit_full": "按指定模式全量生成", "explicit_incremental": "按指定模式增量生成",
        "small_change": "改动和依赖范围较小", "shared_context_changed": "共享配置发生变化",
        "language_changed": "分组语言发生变化", "affected_group_limit": "受影响分组较多",
        "changed_node_limit": "改动节点较多", "affected_group_ratio": "受影响分组占比较高",
        "baseline_unavailable": "缺少可验证的旧画布", "baseline_artifacts_unavailable": "缺少完整旧代码快照"}
    unreachable = (evidence.get("event_binding_validation") or {}).get("unreachable_diagnostics") or []
    diagnostic_lines = []
    if unreachable:
        proof_labels = {"after_return": "无条件 return 后", "after_throw": "无条件 throw 后",
            "constant_false_consequent": "常量条件排除的分支", "constant_true_alternate": "常量条件排除的分支",
            "constant_false_loop": "常量假条件的循环体或更新", "literal_short_circuit": "字面量短路排除的表达式"}
        diagnostic_lines.append("\n静态证明不会执行的引用问题（已记录，不阻断；运行效果未验证）：")
        for finding in unreachable:
            proof = finding["reachability"]
            diagnostic_lines.append(f"分组 {finding['group_key']} · 第 {finding['line']} 行第 {finding['column'] + 1} 列："
                f"{finding['identifier']} {'使用隐式事件实参' if finding.get('reason_code') == 'hidden_event_arguments' else '未声明'}；依据：{proof_labels[proof['reason_code']]}"
                f"（第 {proof['line']} 行第 {proof['column'] + 1} 列）。")
    diagnostics = (evidence.get("event_binding_validation") or {}).get("baseline_diagnostics") or []
    if diagnostics:
        diagnostic_lines.append("\n修改前已存在的代码问题（已检查，未验证；不阻断，是否修复由用户决定）：")
        for finding in diagnostics:
            old = finding.get("baseline") or {}
            reason = {"preexisting_reserved_platform_identifier": "声明或覆盖平台保留名",
                      "preexisting_hidden_event_arguments": "使用隐式事件实参",
                      "preexisting_undeclared_identifier": "未声明"}.get(finding.get("reason_code", "preexisting_undeclared_identifier"), finding.get("reason_code"))
            diagnostic_lines.append(f"分组 {finding['group_key']} · 第 {finding.get('line', '?')} 行："
                f"{finding['identifier']} {reason}；基线第 {old.get('line', '?')} 行。")
    for finding in (evidence.get("event_binding_validation") or {}).get("canvas_diagnostics", []):
        diagnostic_lines.append(f"\n分组 {finding.get('group_key') or '全局变量'} · 节点 {finding.get('node_key') or '声明'} · {finding['path']}："
            + ("显示值与生成表达式不一致" if finding["reason_code"] == "preexisting_paraminput_unsynchronized" else "使用平台保留名")
            + "，与修改前的完整字段一致；已记录、未验证、不阻断，是否修复由用户决定。")
    for check in (evidence.get("event_binding_validation") or {}).get("checks", []):
        issue = check.get("binding_issue")
        if issue:
            reason = {"binding_component_identity_required": "缺少显式组件身份", "event_contract_unavailable": "事件合同不可用",
                      "event_callback_missing": "回调缺失", "event_callback_group_invalid": "回调分组类型或绑定关系无效"}[issue["reason_code"]]
            diagnostic_lines.append(f"\n分组 {check['source_group_key']} · 事件节点 {check['binding_node_key']}：{reason}；"
                "绑定与回调声明和修改前一致，已记录、未验证、不阻断，是否修复由用户决定。")
        for issue in check.get("parameter_issues", []):
            actual = "、".join(f"{param.get('title', '?')}:{param.get('type', '?')}" for param in issue.get("actual_params", [])) or "无"
            expected = "、".join(f"{param.get('title', '?')}:{param.get('type', '?')}" for param in issue.get("expected_params", [])) or "无"
            diagnostic_lines.append(f"\n事件 {check['component_type']}.{check['event_name']} → 分组 {check['target_group_key']}："
                f"{issue['section']} 声明（{actual}）与事件合同（{expected}）不一致，修改前已存在；已记录、不阻断，是否修复由用户决定。"
                "保留原参数名、声明和业务引用，不自动调整。")
    if selection.get("selected_mode") == "full":
        return "\n".join([
            "请求模式：" + labels.get(selection.get("requested_mode"), "全量生成"),
            "实际模式：全量生成", "选择原因：" + reasons.get(selection.get("reason_code"), str(selection.get("reason_code") or "全量生成")),
            f"重新生成全部 {len(selection.get('scope', {}).get('affected_group_keys', [])) + len(selection.get('scope', {}).get('unaffected_group_keys', []))} 个候选动作分组。",
            "完整候选已独立生成核对；词法检查和 C# 编译不代表业务运行验证通过。"] + diagnostic_lines)
    regenerated = generation.get("regenerated_group_keys") or []
    preserved = generation.get("preserved_group_keys") or []
    lines = [f"原生重新生成 {len(regenerated)} 个动作分组；原样保留 {len(preserved)} 个动作分组。",
             "重新生成：" + ("、".join(regenerated) or "无"),
             "原样保留：" + ("、".join(preserved) or "无"),
             "完整候选已独立重建核对；保留旧代码不代表运行验证通过。"]
    if selection:
        lines[:0] = ["请求模式：" + labels.get(selection.get("requested_mode"), "增量生成"),
                     "实际模式：增量生成", "选择原因：" + reasons.get(selection.get("reason_code"), str(selection.get("reason_code") or "增量生成"))]
    return "\n".join(lines + diagnostic_lines)
