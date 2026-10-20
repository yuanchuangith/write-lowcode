"""Bounded, data-only assertions over native canvas parameter values."""
from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any


class CanvasAssertionError(ValueError):
    pass


_ABSENT = object()
_TARGET_STATES = {"matched", "missing", "type_mismatch", "unavailable"}


def validate_json_value(value: Any) -> None:
    """Reject executable/non-JSON values and resource-exhausting specifications."""
    pending = [(value, 0)]
    visited = 0
    while pending:
        current, depth = pending.pop()
        visited += 1
        if depth > 64 or visited > 100000:
            raise CanvasAssertionError("review_spec JSON 深度或项目数量超过限制。")
        if type(current) is dict:
            if any(type(key) is not str for key in current):
                raise CanvasAssertionError("review_spec JSON 对象只能使用文本键。")
            pending.extend((item, depth + 1) for item in current.values())
        elif type(current) is list:
            pending.extend((item, depth + 1) for item in current)
        elif type(current) is float:
            if not math.isfinite(current):
                raise CanvasAssertionError("review_spec 不允许 NaN 或无穷数。")
        elif current is not None and type(current) not in (str, int, bool):
            raise CanvasAssertionError("review_spec 只允许普通 JSON 数据。")


def _text(value, field, limit=256):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise CanvasAssertionError(f"{field} 必须是长度不超过 {limit} 的非空文本。")
    return value


def _tokens(path):
    _text(path, "canvas_assertion.path", 2048)
    if not path.startswith("/") or re.search(r"~(?![01])", path):
        raise CanvasAssertionError("canvas_assertion.path 必须是有效 JSON pointer。")
    tokens = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
    if tokens[0] not in {"inputParams", "outputParams"} or len(tokens) > 64:
        raise CanvasAssertionError("canvas_assertion.path 只能位于 /inputParams 或 /outputParams。")
    return tokens


def normalize_canvas_assertions(assertions, requirements):
    if type(assertions) is not list or len(assertions) > 100:
        raise CanvasAssertionError("canvas_assertions 必须是不超过 100 条的数组。")
    rules = {rule["id"]: rule for rule in requirements}
    result, seen = [], set()
    for item in assertions:
        required = {"id", "requirement_ids", "target", "path", "operator"}
        if type(item) is not dict or not required <= set(item) or set(item) - required - {"value"}:
            raise CanvasAssertionError("画布断言包含缺失或未知字段。")
        identifier = _text(item["id"], "canvas_assertion.id", 128)
        if identifier in seen:
            raise CanvasAssertionError("画布断言 id 不能重复。")
        seen.add(identifier)
        identifiers = item["requirement_ids"]
        if (type(identifiers) is not list or not identifiers or len(identifiers) > 100
                or any(type(key) is not str or key not in rules for key in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            raise CanvasAssertionError("画布断言必须关联不重复且存在的 requirement_ids。")
        target = item["target"]
        if type(target) is not dict or set(target) != {"group_key", "node_key", "element_key"}:
            raise CanvasAssertionError("画布断言 target 必须包含 group_key、node_key、element_key。")
        checked_target = {key: _text(target[key], "canvas_assertion.target." + key) for key in target}
        _tokens(item["path"])
        operator = item["operator"]
        if operator not in ("equals", "contains", "unchanged"):
            raise CanvasAssertionError("画布断言 operator 只能为 equals、contains、unchanged。")
        if (operator == "unchanged") == ("value" in item):
            raise CanvasAssertionError("equals/contains 必须提供 value；unchanged 不能提供 value。")
        native_path = "paramsValue" + item["path"]
        for key in identifiers:
            covered = any(
                scope["group_key"] == target["group_key"]
                and (not scope.get("node_key") or scope["node_key"] == target["node_key"])
                and (not scope.get("field_path") or native_path == scope["field_path"].lstrip("/")
                     or native_path.startswith(scope["field_path"].lstrip("/") + "/")
                     or (operator in {"equals", "unchanged"}
                         and scope["field_path"].lstrip("/").startswith(native_path + "/")))
                for scope in rules[key]["targets"])
            if not covered:
                raise CanvasAssertionError("画布断言目标超出关联需求的 targets 范围。")
        normalized = {"id": identifier, "requirement_ids": list(identifiers), "target": checked_target,
                      "path": item["path"], "operator": operator}
        if "value" in item:
            validate_json_value(item["value"])
            normalized["value"] = deepcopy(item["value"])
        result.append(normalized)
    return result


def _lookup(canvas, assertion):
    if canvas is None:
        return {"target_status": "unavailable", "action_type": None, "present": False, "value": None}
    target = assertion["target"]
    groups = [group for group in canvas["actionData"] if group.get("key") == target["group_key"]]
    if len(groups) != 1:
        return {"target_status": "missing", "action_type": None, "present": False, "value": None}
    group = groups[0]
    nodes = [node for node in group.get("data", []) if node.get("key") == target["node_key"]]
    if len(nodes) != 1:
        return {"target_status": "missing", "action_type": group.get("actionType"), "present": False, "value": None}
    if nodes[0].get("elementKey") != target["element_key"] or group.get("actionType") not in ("js", "csharp"):
        action_type = group.get("actionType") if group.get("actionType") in ("js", "csharp") else None
        return {"target_status": "type_mismatch", "action_type": action_type, "present": False, "value": None}
    value = nodes[0].get("paramsValue", _ABSENT)
    for token in _tokens(assertion["path"]):
        if type(value) is dict:
            value = value.get(token, _ABSENT)
        elif type(value) is list and re.fullmatch(r"0|[1-9][0-9]*", token) and len(token) < 10 and int(token) < len(value):
            value = value[int(token)]
        else:
            value = _ABSENT
        if value is _ABSENT:
            break
    return {"target_status": "matched", "action_type": group["actionType"],
            "present": value is not _ABSENT, "value": None if value is _ABSENT else deepcopy(value)}


def _equal(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(_equal(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def _result(assertion, before, after):
    if after["target_status"] == "unavailable":
        return "unverified", "缺少候选画布，无法核对配置"
    if after["target_status"] != "matched":
        return "failed", "候选目标不存在或元件类型不匹配"
    if before["target_status"] in {"missing", "type_mismatch"}:
        return "failed", "基线目标不存在或元件类型不匹配"
    if before["target_status"] == "matched" and before["action_type"] != after["action_type"]:
        return "failed", "目标分组代码类型发生变化"
    if not after["present"]:
        return "failed", "候选参数路径不存在"
    operator = assertion["operator"]
    if operator == "unchanged":
        if before["target_status"] == "unavailable":
            return "unverified", "缺少基线画布，无法核对保持不变"
        if not before["present"]:
            return "failed", "基线参数路径不存在"
        passed = _equal(before["value"], after["value"])
    elif operator == "equals":
        passed = _equal(after["value"], assertion["value"])
    else:
        if type(after["value"]) is not list:
            return "failed", "contains 仅支持数组中的精确成员"
        passed = any(_equal(value, assertion["value"]) for value in after["value"])
    return ("passed", "配置与固定断言一致；不代表运行验证通过") if passed else ("failed", "配置与固定断言不一致")


def build_canvas_checks(assertions, baseline=None, candidate=None, recorded=None):
    """Recompute provided evidence; canvas values always override recorded values."""
    if recorded is not None and (type(recorded) is not list or len(recorded) != len(assertions)):
        raise CanvasAssertionError("画布配置检查覆盖范围不完整。")
    checks = []
    for index, assertion in enumerate(assertions):
        old = recorded[index] if recorded is not None else None
        before, after = _lookup(baseline, assertion), _lookup(candidate, assertion)
        if old is not None:
            required = {"id", "requirement_ids", "target", "path", "operator", "status", "reason",
                        "before_present", "after_present", "before", "after", "before_target_status",
                        "after_target_status", "before_action_type", "after_action_type"}
            if assertion["operator"] != "unchanged":
                required.add("expected")
            if type(old) is not dict or set(old) != required:
                raise CanvasAssertionError("画布配置检查结构无效。")
            for side, canvas, evidence in (("before", baseline, before), ("after", candidate, after)):
                if (type(old[side + "_present"]) is not bool or type(old[side + "_target_status"]) is not str
                        or old[side + "_target_status"] not in _TARGET_STATES
                        or old[side + "_action_type"] not in (None, "js", "csharp")
                        or not old[side + "_present"] and old[side] is not None
                        or old[side + "_target_status"] != "matched" and old[side + "_present"]
                        or old[side + "_target_status"] == "matched" and old[side + "_action_type"] is None):
                    raise CanvasAssertionError("画布配置检查目标或参数证据无效。")
                validate_json_value(old[side])
                if canvas is None:
                    evidence.update(target_status=old[side + "_target_status"], action_type=old[side + "_action_type"],
                                    present=old[side + "_present"], value=deepcopy(old[side]))
        status, reason = _result(assertion, before, after)
        check = {key: deepcopy(assertion[key]) for key in ("id", "requirement_ids", "target", "path", "operator")}
        check.update(status=status, reason=reason)
        for side, evidence in (("before", before), ("after", after)):
            check.update({side + "_present": evidence["present"], side: evidence["value"],
                          side + "_target_status": evidence["target_status"], side + "_action_type": evidence["action_type"]})
        if "value" in assertion:
            check["expected"] = deepcopy(assertion["value"])
        if old is not None and not _equal(check, old):
            raise CanvasAssertionError("画布配置检查与固定断言或真实画布不一致。")
        checks.append(check)
    return checks
