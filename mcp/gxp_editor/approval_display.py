"""Read-only presentation helpers for the local approval application."""
from __future__ import annotations

import json
import re
from typing import Any

from pymysql.converters import escape_item
from .single_row_checker import check_single_row_statement


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


def _condition(condition: dict[str, Any]) -> str:
    operators = {"Equal": "等于", "NotEqual": "不等于", "EqualNull": "为空", "NotEqualNull": "不为空",
                 "GreaterThan": "大于", "LessThan": "小于", "GreaterThanOrEqual": "大于等于", "LessThanOrEqual": "小于等于"}
    parts = []
    for item in condition.get("Filters", []):
        if "Filters" in item:
            parts.append("（" + _condition(item) + "）")
        else:
            op = str(item.get("equalTo") or item.get("Operator") or "")
            phrase = f"{_label(item.get('target') or item.get('Field'))} {operators.get(op, op)}"
            if op not in {"EqualNull", "NotEqualNull"}:
                phrase += " " + _label(item.get("value"))
            parts.append(phrase)
    return ("，并且 " if str(condition.get("Logic", "And")).lower() == "and" else "，或者 ").join(parts) or "未设置条件"


def canvas_change_summary(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Summarize real parameter/position changes, excluding generated metadata."""
    old_groups = {group["key"]: group for group in before.get("actionData", [])}
    queries: dict[str, str] = {}
    new_flags: set[str] = set()
    for data in (before, after):
        for group in data.get("actionData", []):
            for node in group.get("data", []):
                params = node.get("paramsValue", {})
                inputs, outputs = params.get("inputParams", {}), params.get("outputParams", {})
                if node.get("elementKey") == "QueryData":
                    name = outputs.get("parameterResults", {}).get("code")
                    if name:
                        queries[name] = str(inputs.get("paramName"))
                variable_value = inputs.get("variableValue")
                if isinstance(variable_value, dict) and variable_value.get("code") == '(inbiz.getPageMode()=="add")':
                    new_flags.add(str(outputs.get("variableName", {}).get("code")))

    def source(value: Any, target: str) -> str:
        if isinstance(value, dict):
            code = str(value.get("code") or "")
            if code in queries:
                return f"直接使用页面参数“{queries[code]}”覆盖“{target}”"
            match = re.fullmatch(r"\((\w+) && !relatedAttributes\.isEmpty\((\w+)\)\)\s*\?\s*(\w+)\s*:\s*(.+)", code)
            if match and match[1] in new_flags and match[2] == match[3] and match[2] in queries:
                output_code = target_codes.get(target)
                if match[4] == output_code:
                    return f"仅在新增申请、且页面参数“{queries[match[2]]}”非空时赋值；重新打开已有申请时保留“{target}”原值"
            if value.get("paramTypes") == "custom" and len(code) > 100:
                return "使用新的自定义计算规则（完整表达式见“原始节点详情”）"
        return _label(value)

    target_codes: dict[str, str] = {}
    chunks: list[str] = []
    count = 0
    for group in after.get("actionData", []):
        old_group = old_groups.pop(group["key"], {})
        old_keys = [node["key"] for node in old_group.get("data", [])]
        new_keys = [node["key"] for node in group.get("data", [])]
        common = set(old_keys) & set(new_keys)
        old_order = [key for key in old_keys if key in common]
        new_order = [key for key in new_keys if key in common]
        moved = {key for position, key in enumerate(new_order) if old_order[position] != key}
        old_nodes = {node["key"]: (line, node) for line, node in enumerate(old_group.get("data", []), 1)}
        for line, node in enumerate(group.get("data", []), 1):
            old_line, old = old_nodes.pop(node["key"], (None, None))
            if old is not None and old.get("elementKey") == node.get("elementKey") and old.get("paramsValue") == node.get("paramsValue") and node["key"] not in moved:
                continue
            count += 1
            location = f"{group.get('title') or group['key']} → 第 {line} 行 → {node.get('title') or node['elementKey']}"
            if old is None:
                chunks.append(f"{count}. 新增节点\n位置：{location}\n内容：{node.get('description') or '新增一项操作'}")
                continue
            if old.get("paramsValue") == node.get("paramsValue") and old.get("elementKey") == node.get("elementKey"):
                chunks.append(f"{count}. 移动节点\n位置：{location}\n修改前：第 {old_line} 行\n修改后：第 {line} 行")
                continue
            inputs = node.get("paramsValue", {}).get("inputParams", {})
            old_inputs = old.get("paramsValue", {}).get("inputParams", {})
            if node["elementKey"] in {"SetVariable", "SetVariableValue"} and "variableValue" in inputs:
                target_value = node.get("paramsValue", {}).get("outputParams", {}).get("variableName", {})
                target = _label(target_value)
                target_codes[target] = str(target_value.get("code") or "")
                detail = f"修改字段：{target}\n修改前：{source(old_inputs.get('variableValue'), target)}\n修改后：{source(inputs.get('variableValue'), target)}"
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
                            changes.append(f"字段“{field}”\n修改前：{_label(previous.get(field))}\n修改后：{_label(current.get(field))}")
                detail = "\n".join(changes) or "节点类型发生变更，完整配置见“原始节点详情”。"
            if node["elementKey"] == "SetVariable":
                nodes = group.get("data", [])
                previous = nodes[line - 2] if line > 1 else None
                following = nodes[line] if line < len(nodes) else None
                name = lambda adjacent: (adjacent.get("title") or adjacent.get("elementKey")) if adjacent else "无（分组边界）"
                detail += (
                    f"\n草稿查看：打开该动作草稿 → {location} → 元件配置 → 输入参数 → 变量值"
                    f"\n相邻元件：上一行“{name(previous)}”；下一行“{name(following)}”"
                )
                value = inputs.get("variableValue")
                if isinstance(value, dict) and value.get("paramTypes") == "custom":
                    detail += f"\n配置方式：自定义表达式（在原元件内，未新增 IF 元件）\n变量值（完整）：{value.get('value', '未设置')}"
                    if value.get("value") != value.get("code"):
                        detail += "\n警告：显示值与生成表达式不一致，重新保存可能覆盖修改。"
            chunks.append(f"{count}. 修改节点\n位置：{location}\n{detail}")
        for old_line, old in old_nodes.values():
            count += 1
            chunks.append(f"{count}. 删除节点\n位置：{old_group.get('title') or group['key']} → 第 {old_line} 行 → {old.get('title') or old['elementKey']}")
    for group in old_groups.values():
        count += 1
        chunks.append(f"{count}. 删除分组“{group.get('title') or group['key']}”，包含 {len(group.get('data', []))} 个节点")
    if before.get("globalVariables") != after.get("globalVariables"):
        count += 1
        chunks.append(f"{count}. 全局变量发生变更，完整内容见“原始节点详情”。")
    return f"本次共 {count} 项画布变更\n\n" + "\n\n".join(chunks) if count else "画布参数未变更。"


def parameter_details(statement: dict[str, Any]) -> str:
    chunks = ["原始参数化 SQL", statement["sql"], "\n完整参数（未截断）"]
    for index, value in enumerate(statement["params"], 1):
        chunks.extend([f"\n参数 {index}", value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)])
    return "\n".join(chunks)


def execution_result_message(result: dict[str, Any]) -> str:
    if result.get("state") == "saved":
        return (
            "草稿已保存成功。\n\n"
            "当前运行仍使用发布副本，此次保存尚未生效。\n"
            "请在平台检查该动作的草稿并手动发布，再重新打开业务页面验证。\n\n"
            "保存完成后还需由编辑工具核对草稿。"
        )
    return "事务结果：\n" + json.dumps(result, ensure_ascii=False, indent=2, default=str)
