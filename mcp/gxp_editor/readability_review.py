"""Candidate-bound readability findings and native note placement checks."""
from __future__ import annotations

from .hashing import sha256_json


POLICY_VERSION = 1
NOTE_PATH = "paramsValue/inputParams/content"
_GUARDS = {"If", "IfCondition", "ElseIfCondition", "Else"}
_CONTINUATIONS = {"ElseIfCondition", "Else"}
_WRITES = {"AddNewData", "AddNewDataByDict", "UpdateData", "UpdateDataByDict", "DeleteData", "SetProcessVariables"}
_WRITE_METHODS = {"setValue", "setValueToID", "setValues"}
_FINDING_FIELDS = {"id", "rule", "status", "group_key", "node_key", "field_path", "reason"}
_NOTE_RULES = {"required_native_comment", "critical_write_comment", "invalid_native_comment"}


def normalize_comment_blocks(blocks, requirements):
    if type(blocks) is not list or len(blocks) > 100:
        raise ValueError("comment_blocks 须为最多 100 项的数组。")
    targets = {row["id"]: row["targets"] for row in requirements}
    normalized, seen = [], set()
    required = {"id", "requirement_id", "group_key"}
    for row in blocks:
        if (type(row) is not dict or not required <= row.keys() or row.keys() - required - {"anchor_node_key"}
                or any(type(value) is not str or not value.strip() or len(value) > (128 if key in {
                    "id", "requirement_id"} else 256) for key, value in row.items())):
            raise ValueError("comment_blocks 仅允许 id、requirement_id、group_key 和可选的 anchor_node_key。")
        if row["id"] in seen:
            raise ValueError("comment_blocks 的 id 不能重复。")
        seen.add(row["id"])
        anchor = row.get("anchor_node_key", "")
        if not any(target["group_key"] == row["group_key"] and target.get("node_key", "") in ("", anchor)
                   for target in targets.get(row["requirement_id"], ())):
            raise ValueError("注释位置须关联现有需求，且不能超出该需求的分组或节点范围。")
        normalized.append(dict(row))
    return normalized


def _groups(canvas):
    return {row["key"]: row.get("data", []) for row in canvas["actionData"]}


def _depth(node):
    depth = node.get("depth")
    return depth if type(depth) is list else []


def _inputs(node):
    return (node.get("paramsValue") or {}).get("inputParams") or {}


def _content_valid(content):
    return (isinstance(content, str) and bool(content.strip())
            and not any(ch in content for ch in "\r\n\x85\u2028\u2029"))


def _valid_note(node, depth):
    return node.get("elementKey") == "CustomExt" and _depth(node) == depth and _content_valid(_inputs(node).get("content"))


def _has_note(nodes, anchor):
    if not anchor:
        return bool(nodes) and _valid_note(nodes[0], [])
    for index, node in enumerate(nodes):
        if node.get("key") != anchor:
            continue
        if node.get("elementKey") in _CONTINUATIONS:
            return (index + 1 < len(nodes) and _valid_note(nodes[index + 1], [*_depth(node)[:-1], anchor]))
        return bool(index) and _valid_note(nodes[index - 1], _depth(node))
    return False


def _signature(node):
    return (node.get("elementKey"), node.get("paramsValue"))


def _guards(node, nodes):
    chain = []
    for key in _depth(node):
        parent = nodes.get(key) or {}
        kind = parent.get("elementKey")
        if kind not in _GUARDS:
            continue
        # ELSE markers replace the preceding arm at the same logical level.
        if kind in _CONTINUATIONS and chain and _depth(parent)[-1:] == [chain[-1][0]]:
            chain.pop()
        chain.append((key, parent))
    return chain


def _critical_write(node):
    kind = node.get("elementKey")
    method = _inputs(node).get("methodName") if kind == "ComponentMethod" else None
    return (kind in _WRITES or kind == "ComponentMethod"
            and isinstance(method, str) and method in _WRITE_METHODS)


def _changed_notes(baseline, candidate):
    old_groups = _groups(baseline)
    changed = []
    for group, nodes in _groups(candidate).items():
        old = {node.get("key"): node for node in old_groups.get(group, ())}
        for node in nodes:
            if node.get("elementKey") == "CustomExt":
                previous = old.get(node["key"], {})
                if _signature(previous) != _signature(node) or _depth(previous) != _depth(node):
                    changed.append({"group_key": group, "node_key": node["key"]})
    return changed


def _automatic_blocks(baseline, candidate):
    old_groups = _groups(baseline)
    blocks = set()
    for group, nodes in _groups(candidate).items():
        old = {node.get("key"): node for node in old_groups.get(group, ())}
        current = {node.get("key"): node for node in nodes}
        for node in nodes:
            if not _critical_write(node):
                continue
            chain = _guards(node, current)
            if len(chain) < 2:
                continue
            previous = old.get(node.get("key")) or {}
            old_chain = _guards(previous, old)
            if _signature(previous) == _signature(node) and [
                    _signature(parent) for _, parent in old_chain] == [_signature(parent) for _, parent in chain]:
                continue
            first_changed = next((key for index, (key, parent) in enumerate(chain) if index >= len(old_chain)
                                  or _signature(parent) != _signature(old_chain[index][1])), chain[0][0])
            blocks.add((group, first_changed))
    return [{"group_key": group, "anchor_node_key": anchor} for group, anchor in sorted(blocks)]


def _finding(rule, group, node, reason, *, identifier="", requirement_id=""):
    finding = {"rule": rule, "status": "failed", "group_key": group, "node_key": node,
               "field_path": NOTE_PATH, "reason": reason}
    finding["id"] = identifier or sha256_json(finding)[:24]
    if requirement_id:
        finding["requirement_id"] = requirement_id
    return finding


def _note_findings(spec, candidate, automatic, changed):
    groups = _groups(candidate)
    findings = []
    explicit = spec.get("comment_blocks", [])
    explicit_anchors = {(block["group_key"], block.get("anchor_node_key", "")) for block in explicit}
    for block in explicit:
        group, anchor = block["group_key"], block.get("anchor_node_key", "")
        if not _has_note(groups.get(group, []), anchor):
            findings.append(_finding("required_native_comment", group, anchor,
                "在指定入口添加同分支、紧邻且非空的单行 CustomExt 业务注释。",
                identifier="comment:" + block["id"], requirement_id=block["requirement_id"]))
    for block in automatic:
        group, anchor = block["group_key"], block["anchor_node_key"]
        if (group, anchor) not in explicit_anchors and not _has_note(groups.get(group, []), anchor):
            findings.append(_finding("critical_write_comment", group, anchor,
                "在本次新增或改动的多层写入规则入口添加单行 CustomExt，说明触发原因或跳过条件。"))
    nodes = {(group, node["key"]): node for group, entries in groups.items() for node in entries
             if node.get("elementKey") == "CustomExt"}
    for row in changed:
        group, key = row["group_key"], row["node_key"]
        if (group, key) not in nodes:
            raise ValueError("改动的原生注释与当前画布不一致。")
        if not _content_valid(_inputs(nodes[group, key]).get("content")):
            findings.append(_finding("invalid_native_comment", group, key,
                "CustomExt 内容必须是非空的单行文本，不能包含换行。"))
    return findings


def _analyzer_findings(baseline, candidate, frontend_root):
    from .readability import analyze_readability

    findings = {}
    for row in analyze_readability(baseline, candidate, frontend_root=frontend_root):
        identity = {key: row[key] for key in ("rule", "group_key", "node_key", "field_path")}
        identifier = sha256_json(identity)[:24]
        findings.setdefault(identifier, {**row, "id": identifier})
    return list(findings.values())


def build_readability(spec, baseline, candidate, *, frontend_root=None):
    automatic = _automatic_blocks(baseline, candidate)
    changed = _changed_notes(baseline, candidate)
    return {"policy_version": POLICY_VERSION, "automatic_comment_blocks": automatic,
            "changed_comment_nodes": changed,
            "findings": [*_note_findings(spec, candidate, automatic, changed), *_analyzer_findings(baseline, candidate, frontend_root)]}


def _valid_targets(targets, node_field):
    return (type(targets) is list and all(type(row) is dict and row.keys() == {"group_key", node_field}
        and all(type(value) is str and value for value in row.values()) for row in targets)
        and len({(row["group_key"], row[node_field]) for row in targets}) == len(targets))


def validate_readability(report, spec, candidate=None, baseline=None):
    if (type(report) is not dict or report.keys() != {"policy_version", "automatic_comment_blocks", "findings", "changed_comment_nodes"}
            or type(report["policy_version"]) is not int or report["policy_version"] != POLICY_VERSION):
        raise ValueError("编排可读性检查版本或结构无效，请重新生成计划。")
    automatic, findings, changed = report["automatic_comment_blocks"], report["findings"], report["changed_comment_nodes"]
    if (not _valid_targets(automatic, "anchor_node_key") or not _valid_targets(changed, "node_key")
            or type(findings) is not list or any(type(row) is not dict or not _FINDING_FIELDS <= row.keys()
                or any(type(row[key]) is not str for key in _FINDING_FIELDS) or not row["id"]
                or row["status"] not in {"failed", "warning"} for row in findings)
            or len({row["id"] for row in findings}) != len(findings)):
        raise ValueError("编排可读性检查结果无效。")
    if candidate is not None:
        notes = [row for row in findings if row["rule"] in _NOTE_RULES]
        if (notes != _note_findings(spec, candidate, automatic, changed)
                or (baseline is not None and (automatic != _automatic_blocks(baseline, candidate)
                                              or changed != _changed_notes(baseline, candidate)))):
            raise ValueError("原生注释要求与当前画布证据不一致。")
    return report
