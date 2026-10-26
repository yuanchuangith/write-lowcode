"""Consistent synthetic receipts for service/target unit tests, not business evals."""
from copy import deepcopy


def passed_receipt(expect, trace=None):
    trace = deepcopy(trace or [])
    counts = {}
    for entry in trace:
        counts[entry["api"]] = counts.get(entry["api"], 0) + 1
    actual = {"fields": deepcopy(expect.get("fields", {})),
              "field_value_presence": {name: "null" if field["value"] is None else "value"
                                       for name, field in expect.get("fields", {}).items()},
              "metadata": deepcopy(expect.get("metadata")),
              "metadata_registrations": counts.get("metadata.register", 0), "call_counts": counts}
    assertions = []

    def add(name, value, expected):
        kind = "null" if value is None else {bool: "boolean", int: "number", float: "number",
                                               str: "string", dict: "object", list: "object"}[type(value)]
        assertions.append({"name": name, "passed": True, "actual_present": True,
                           "actual_kind": kind, "actual": deepcopy(value), "expected": deepcopy(expected)})

    for name, field in expect.get("fields", {}).items():
        add("fields." + name + ".value", field["value"], field["value"])
    for name in ("metadata", "metadata_registrations", "last_return"):
        if name in expect:
            actual[name] = deepcopy(expect[name])
            add(name, actual[name], expect[name])
    if "last_return" not in expect:
        actual["last_return_undefined"] = True
    if expect.get("last_return_undefined"):
        add("last_return_undefined", True, True)
    for name, count in expect.get("call_counts", {}).items():
        add("call_counts." + name, counts.get(name, 0), count)
    if "call_order" in expect:
        add("call_order", [entry["api"] for entry in trace], expect["call_order"])
    return {"status": "passed", "trace": trace, "actual": actual, "assertions": assertions}
