"""Six synthetic complete MCP conversations; bytes only, no business IO/execution.

Real FastMCP transport, EditorService, state, backup, preflight, business review
and approval artifacts; repository/compiler/runtime/approval launch are simulated.
This does not measure model latency, real code generation, SQL or human approval.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.payload_fixtures import PayloadHarness
from gxp_editor.approval import load_artifact
from gxp_editor.edit_guards import parameter_value
from gxp_editor.hashing import sha256_json
from gxp_editor.logic_review import plan_binding_hash


def fixtures():
    base = PayloadHarness().canvas
    rows = []
    for name in ("long_chinese", "nested_filters", "native_array_rows", "large_parent", "large_native_contract", "large_review_values"):
        canvas = deepcopy(base)
        settings = canvas["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["settings"]
        path, selector, value, padding = "/inputParams/settings/flag", None, True, ""
        if name == "long_chinese":
            settings["notes"] = "配置备注中文" * 12000
        elif name == "nested_filters":
            settings["filters"] = [{"field": str(i), "operator": "equals", "value": "筛选条件" * 50} for i in range(150)]
        elif name == "native_array_rows":
            target = canvas["actionData"][0]["data"][0]
            target["paramsValue"]["inputParams"]["rows"] = [{"id": f"r{i}", "name": "original", "notes": "配置" * 80} for i in range(250)]
            path, selector = "/inputParams/rows", {"field": "id", "value": "r10"}
            value = {**target["paramsValue"]["inputParams"]["rows"][10], "name": "updated"}
        elif name == "large_parent":
            parent = {"key": "parent", "elementKey": "If", "title": "Parent", "depth": [],
                      "paramsValue": {"inputParams": {"condition": "true", "notes": "父配置" * 15000}, "outputParams": {}}}
            canvas["actionData"][0]["data"].insert(0, parent)
            canvas["actionData"][0]["data"][1]["depth"] = ["parent"]
        elif name == "large_native_contract":
            padding = "原生默认配置" * 10000
        else:
            settings["notes"] = "历史配置" * 16000
        rows.append((name, canvas, path, selector, value, padding))
    return rows


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


async def conversation(server, fixture, detail):
    name, canvas, path, selector, new_value, padding = fixture
    with PayloadHarness(canvas, padding) as harness, patch.object(server, "service", return_value=harness.service):
        phases = []
        async def call(tool, **arguments):
            arguments["conversation_id"] = harness.owner
            content, structured = await server.mcp.call_tool(tool, arguments)
            if structured.get("blocked") or structured.get("error"):
                raise RuntimeError(tool + " failed: " + str(structured.get("error")))
            blocks = [item.model_dump(mode="json", exclude_none=True) for item in content]
            phases.append({"tool": tool, "structured_utf8_bytes": _bytes(structured),
                           "text_and_structured_utf8_bytes": _bytes({"content": blocks, "structuredContent": structured})})
            return structured
        begin = await call("begin_edit_session", identifier="ActionOne", **({"response_detail": "full"} if detail == "full" else {}))
        sid = begin["session_id"]
        await call("find_edit_nodes", session_id=sid, query="Target", limit=1)
        options = {"response_detail": "full"} if detail == "full" else {"parameter_paths": [path]}
        context = await call("inspect_edit_context", session_id=sid, targets=[{"group_key": "main", "node_key": "target"}], **options)
        # Both flows read the same needed field/row. Unrelated config is not
        # reread, and complete fragments are never substituted for an old value.
        read = await call("read_edit_value", session_id=sid, group_key="main", node_key="target", path=path, item_key=selector)
        operation = {"operation": "set_array_item" if selector else "set_parameter", "action_group_key": "main",
            "target_node_key": "target", "expected_element_key": "Noop", "path": path, "value": new_value,
            **({"item_key": selector} if selector else {})}
        if detail == "full":
            operation["expected_old_value"] = parameter_value(canvas, "main", "target", path, selector)
        else:
            operation["guard_ref"] = read["guard_ref"]
        plan = await call("plan_canvas_patch", session_id=sid, operations=[operation], review_spec=harness.spec(),
                          **({"response_detail": "full"} if detail == "full" else {}))
        plan_artifact = load_artifact(harness.service.state.get_plan(plan["plan_id"])["artifact_path"])
        await call("read_plan_review", plan_id=plan["plan_id"], section="changes", limit=1,
                   **({"detail": "full"} if detail == "full" else {}))
        pending = await call("prepare_write", plan_id=plan["plan_id"], **({"response_detail": "full"} if detail == "full" else {}))
        artifact = load_artifact(harness.service.state.get_transaction(pending["transaction_id"])["artifact_path"])
        return {"detail": detail, "calls": len(phases), "phases": phases,
            "structured_utf8_bytes": sum(row["structured_utf8_bytes"] for row in phases),
            "text_and_structured_utf8_bytes": sum(row["text_and_structured_utf8_bytes"] for row in phases),
            "candidate_sha256": sha256_json(plan_artifact["desired"]),
            "bound_plan_sha256": plan_binding_hash(plan_artifact),
            "approval_candidate_sha256": sha256_json(artifact["desired_design"]),
            "approval_has_no_consent": artifact["approvals"] == {},
            "full_review_sha256": sha256_json(artifact["business_review"]),
            "only_expanded_operations_reach_compiler": all("guard_ref" not in op and "expected_old_value" in op
                for batch in harness.compiler.seen_operations for op in batch)}


async def measure():
    spec = importlib.util.spec_from_file_location("payload_benchmark_tool_server", ROOT / "mcp" / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    logging.getLogger("gxp.performance").setLevel(logging.WARNING)
    results = []
    for fixture in fixtures():
        full = await conversation(server, fixture, "full")
        summary = await conversation(server, fixture, "summary")
        equal = all(full[key] == summary[key] for key in ("candidate_sha256", "bound_plan_sha256", "approval_candidate_sha256", "full_review_sha256"))
        if not equal or not all(row["approval_has_no_consent"] and row["only_expanded_operations_reach_compiler"] for row in (full, summary)):
            raise RuntimeError("Bound synthetic evidence changed: " + fixture[0])
        ratio = 1 - summary["text_and_structured_utf8_bytes"] / full["text_and_structured_utf8_bytes"]
        if ratio < .5:
            raise RuntimeError("Complete conversation did not reach the 50% byte reduction target: " + fixture[0])
        results.append({"scenario": fixture[0], "full": full, "summary": summary, "bound_evidence_equal": equal,
            "text_and_structured_reduction_percent": round(ratio * 100, 3)})
    return {"scope": "six synthetic complete MCP conversations; simulated repository/compiler/runtime/UI; real service/state/backup/preflight/review/approval artifacts",
        "unmeasured": ["real compiler latency", "database", "network", "model tokenization/latency", "business execution", "human approval"],
        "byte_method": "UTF-8 compact JSON of the actual FastMCP structured response (including trace/timings), and of both actual text blocks plus structuredContent. SDK wire framing is not measured.",
        "budget_scope": "read_edit_value max_output_bytes bounds its service payload, including guard/binding. Trace/timings and MCP SDK wrapping are additional.",
        "comparison": "explicit full compatibility flow versus default-summary flow; identical seven calls and selected exact read; no latency speedup claim",
        "scenarios": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(measure())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{ "scenario": row["scenario"], "reduction_percent": row["text_and_structured_reduction_percent"]} for row in result["scenarios"]], ensure_ascii=False))


if __name__ == "__main__":
    main()
