"""Shared scope paths and native-generated identities used by business review."""
from __future__ import annotations

from copy import deepcopy


def scope_path(path):
    return (path or "").lstrip("/")


def path_covers(parent, child):
    parent, child = scope_path(parent), scope_path(child)
    return not parent or child == parent or child.startswith(parent + "/")


def native_alias_bindings(diff):
    bindings = {"groups": {}, "nodes": {}}
    for row in diff:
        if row.get("group_alias"):
            alias = row["group_alias"]
            if alias in bindings["groups"]:
                raise ValueError("Duplicate native group alias")
            bindings["groups"][alias] = row["action_group_key"]
        if row.get("node_alias"):
            alias = row["node_alias"]
            if alias in bindings["nodes"] or not row.get("generated_nodes"):
                raise ValueError("Invalid native node alias")
            bindings["nodes"][alias] = {"group_key": row["action_group_key"],
                                        "node_key": row["generated_nodes"][0]["key"]}
    return bindings


def resolve_review_targets(spec, bindings, candidate=None):
    """Resolve only declared aliases; never guess identities from titles or code."""
    result = deepcopy(spec)
    if (type(bindings) is not dict or set(bindings) != {"groups", "nodes"}
            or any(type(bindings[key]) is not dict for key in bindings)):
        raise ValueError("Invalid native review alias bindings")
    groups = {group["key"]: group for group in candidate["actionData"]} if candidate is not None else None

    def group_key(key):
        if not key.startswith("@"):
            return key
        resolved = bindings["groups"].get(key[1:])
        if not isinstance(resolved, str) or not resolved or groups is not None and resolved not in groups:
            raise ValueError("Review group alias does not identify a current native group: " + key)
        return resolved

    def target(item):
        item["group_key"] = group_key(item["group_key"])
        key = item.get("node_key", "")
        if key.startswith("@"):
            resolved = bindings["nodes"].get(key[1:])
            if (not isinstance(resolved, dict) or resolved.get("group_key") != item["group_key"]
                    or not isinstance(resolved.get("node_key"), str) or not resolved["node_key"]
                    or groups is not None and not any(node.get("key") == resolved["node_key"]
                               for node in groups.get(item["group_key"], {}).get("data", []))):
                raise ValueError("Review node alias is missing, stale or belongs to another group: " + key)
            item["node_key"] = resolved["node_key"]

    def api_name(name):
        return "group." + group_key(name[6:]) if name.startswith("group.@") else name

    for rule in result["requirements"]:
        for item in rule["targets"]:
            target(item)
    for assertion in result.get("canvas_assertions", []):
        target(assertion["target"])
    for case in result["scenarios"]["cases"]:
        for step in case["steps"]:
            if step["kind"] == "call_group":
                step["group_key"] = group_key(step["group_key"])
        expected = case.get("expect", {})
        if "call_order" in expected:
            expected["call_order"] = [api_name(name) for name in expected["call_order"]]
        if "call_counts" in expected:
            expected["call_counts"] = {api_name(name): value for name, value in expected["call_counts"].items()}
    return result


def requirements_spec(review):
    return review.get("source_spec", review["spec"])


def only_invalid_locator_corrections(previous, current, canvas, operations=()):
    """A missing identity can be corrected without changing the user's rules.

    Existing targets, cardinality, field scope, text and source evidence cannot
    be widened through this exception. All candidate changes still need coverage.
    """
    group_aliases = {"@" + op["group_alias"] for op in operations if op.get("operation") == "create_action_group"}
    node_aliases = {"@" + op["node_alias"]: op["action_group_key"] for op in operations if op.get("node_alias")}

    def exists(target):
        if target["group_key"] in group_aliases:
            return not target.get("node_key") or node_aliases.get(target["node_key"]) == target["group_key"]
        groups = [group for group in canvas["actionData"] if group["key"] == target["group_key"]]
        if target.get("node_key") in node_aliases:
            return len(groups) == 1 and node_aliases[target["node_key"]] == target["group_key"]
        return len(groups) == 1 and (not target.get("node_key") or sum(
            node["key"] == target["node_key"] for node in groups[0].get("data", [])) == 1)

    old = {rule["id"]: rule for rule in previous["requirements"]}
    new = {rule["id"]: rule for rule in current["requirements"]}
    if old.keys() != new.keys():
        return False
    for key, rule in old.items():
        replacement = new[key]
        if any(rule[field] != replacement[field] for field in ("text", "source_text")):
            return False
        if len(rule["targets"]) != len(replacement["targets"]):
            return False
        for before, after in zip(rule["targets"], replacement["targets"]):
            if before == after:
                continue
            if (exists(before) or not exists(after) or before.get("field_path", "") != after.get("field_path", "")
                    or bool(before.get("node_key")) != bool(after.get("node_key"))):
                return False
    return True
