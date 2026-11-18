"""Differential review of authored canvas expressions. Never executes them."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


_VALUE = {"SetVariable": "variableValue", "SetVariableValue": "attributeValue"}
_IF = {"IfCondition", "ElseIfCondition"}
_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*\Z")
_TRIVIAL = re.compile(r"\s*(?:[\w$]+|[+-]?\d+(?:\.\d+)?)\s*\Z")
_MAX_ANALYSIS_BYTES = 1024 * 1024


def _params(node, section):
    return (node.get("paramsValue") or {}).get(section) or {}


def _code(value):
    if not isinstance(value, dict):
        return ""
    return value.get("code") if isinstance(value.get("code"), str) else ""


def _condition(node):
    if node.get("elementKey") not in _IF:
        return {}
    return _params(node, "inputParams").get("condition") or {}


def _one_leaf(condition):
    while isinstance(condition, dict) and isinstance(condition.get("Filters"), list):
        children = condition["Filters"]
        if len(children) != 1:
            return {}
        condition = children[0]
    return condition if isinstance(condition, dict) else {}


def _predicate(condition):
    leaf = _one_leaf(condition)
    if leaf.get("equalTo") == "Equal" and _code(leaf.get("value")).strip() == "true":
        return _code(leaf.get("target"))
    if not leaf.get("equalTo"):
        return _code(leaf.get("target"))
    return ""


def _condition_expressions(condition, path="/paramsValue/inputParams/condition"):
    if not isinstance(condition, dict):
        return
    for field in ("target", "value"):
        if code := _code(condition.get(field)):
            yield path + "/" + field + "/code", code
    for index, child in enumerate(condition.get("Filters") or []):
        yield from _condition_expressions(child, f"{path}/Filters/{index}")


def _single_use(name, group, nodes, definition, consumer):
    # Reject any apparent use in other authored parameters, even a textual
    # false positive. We only block when we can show a disposable wrapper.
    match = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
    values = [group.get("inputParams"), group.get("outputParams")]
    values.extend(n.get("paramsValue") for n in nodes if n is not definition and n is not consumer)
    return not any(match.search(json.dumps(v, ensure_ascii=False)) for v in values)


def _jobs(baseline, candidate):
    old_groups = {group["key"]: group for group in baseline.get("actionData", [])}
    for group in candidate.get("actionData", []):
        if group.get("actionType") not in ("js", "csharp"):
            continue
        old_group = old_groups.get(group["key"], {})
        old = {node["key"]: node for node in old_group.get("data", [])}
        nodes = group.get("data", [])
        for index, node in enumerate(nodes):
            key, kind = node["key"], node.get("elementKey")
            before = old.get(key, {})
            same_place = (kind == before.get("elementKey") and (node.get("depth") or []) == (before.get("depth") or [])
                          and group.get("actionType") == old_group.get("actionType"))
            base = {"group_key": group["key"], "node_key": key, "language": group["actionType"]}
            if field := _VALUE.get(kind):
                code = _code(_params(node, "inputParams").get(field))
                if not code or (same_place and code == _code(_params(before, "inputParams").get(field))):
                    continue
                options = {"value_expression": True}
                if kind == "SetVariable" and index + 1 < len(nodes):
                    consumer = nodes[index + 1]
                    name = _code(_params(node, "outputParams").get("variableName")).strip()
                    if (_NAME.fullmatch(name) and _predicate(_condition(consumer)).strip() == name
                            and (node.get("depth") or []) == (consumer.get("depth") or [])
                            and _single_use(name, group, nodes, node, consumer)):
                        options.update(wrapper=True, previous_predicate=_predicate(_condition(old.get(consumer["key"], {}))))
                yield {**base, **options, "field_path": f"/paramsValue/inputParams/{field}/code", "code": code}
            elif kind in _IF:
                previous = dict(_condition_expressions(_condition(before))) if same_place else {}
                for path, code in _condition_expressions(_condition(node)):
                    if previous.get(path) != code:
                        yield {**base, "field_path": path, "code": code}


def analyze_readability(baseline_canvas, candidate_canvas, *, frontend_root=None):
    jobs = list(_jobs(baseline_canvas, candidate_canvas))
    warnings, js = [], []
    for job in jobs:
        if job["language"] == "js" and not _TRIVIAL.fullmatch(job["code"]):
            js.append(job)
        elif len(job["code"]) >= 160:
            warnings.append(_finding(job, "complex_expression_unverified", "C# 复杂表达式需复核职责和原生拆分边界。"))
    if not js:
        return warnings
    payload = json.dumps(js, ensure_ascii=False).encode("utf-8")
    if len(payload) <= _MAX_ANALYSIS_BYTES:
        try:
            result = subprocess.run(
                ["node", "--max-old-space-size=128", str(Path(__file__).with_name("readability_ast.cjs")),
                 str(frontend_root or "")], input=payload, capture_output=True, timeout=4, check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            rows = json.loads(result.stdout)
            if isinstance(rows, list):
                return warnings + rows
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    # The generator may be mocked in offline tests. Never claim an analysis
    # passed if the parser or budget was unavailable.
    return warnings + [_finding(job, "readability_analysis_unavailable", "静态检查未完成；请核对表达式是否隐藏了判断或写入。")
                       for job in js]


def _finding(job, rule, reason):
    return {**{field: job[field] for field in ("group_key", "node_key", "field_path")},
            "rule": rule, "status": "warning", "reason": reason}
