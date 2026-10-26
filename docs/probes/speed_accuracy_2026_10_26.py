"""Synthetic report probe: no credentials, business reads, writes, or code execution."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp"))
from gxp_editor.compiler import CompilerBridge
from gxp_editor.config import DatabaseConfig, EditorConfig, RuntimeReferenceConfig
from gxp_editor.telemetry import traced_tool


def fingerprint(value):
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def run(frontend: Path):
    config = EditorConfig(database=DatabaseConfig("127.0.0.1", 1, "unused", "unused"),
        frontend_repo=str(frontend), compiler_bridge="dist/action-design-compiler/bridge.js",
        runtime_references=RuntimeReferenceConfig(str(frontend), target_framework="net9.0"))
    record = lambda value: {"paramTypes": "custom", "value": value, "code": value,
                            "label": "value", "dataType": "number"}
    empty = {"actionData": [{"key": f"g{i}", "title": f"Group {i}", "actionType": "js", "data": []}
                            for i in range(6)], "globalVariables": [], "code": "OfflineModeProbe"}
    initial = [{"operation": "append_node", "action_group_key": f"g{i}", "element_key": "SetVariable",
                "input_params": {"variableType": "number", "variableValue": record("1")},
                "output_params": {"variableName": {**record(f"answer{i}"), "label": f"answer{i}"}}}
               for i in range(6)]
    baseline = CompilerBridge(config).compile_patch(empty, initial)
    design = baseline["design"]
    artifacts = {key: baseline[key] for key in ("js_code", "csharp_code", "relation_data")}
    node = design["actionData"][0]["data"][0]
    operations = [{"operation": "set_parameter", "action_group_key": "g0",
        "target_node_key": node["key"], "expected_element_key": "SetVariable",
        "path": "/inputParams/variableValue",
        "expected_old_value": deepcopy(node["paramsValue"]["inputParams"]["variableValue"]),
        "value": record("2")}]
    calls = Counter()

    class MeasuredBridge(CompilerBridge):
        def _call(self, payload, **kwargs):
            calls[payload["command"]] += 1
            return super()._call(payload, **kwargs)

    @traced_tool
    def trial(mode):
        engine = MeasuredBridge(config)
        candidate = engine.compile_patch(design, operations, baseline_design=design,
            baseline_artifacts=artifacts, generation_mode=mode)
        evidence = engine.verify_compiled_result(candidate)
        preserved = candidate["design"]["actionData"][1:] == design["actionData"][1:]
        if not preserved or not evidence["generated_code_consistency_verified"]:
            raise RuntimeError("Synthetic integrity check failed")
        selection = candidate.get("generation_selection", {})
        return {"blocked": False, "requested_mode": mode,
            "selected_mode": selection.get("selected_mode"),
            "selection_reason": selection.get("reason_code"),
            "artifact_sha256": {key: fingerprint(candidate[key]) for key in artifacts},
            "binding_sha256": fingerprint(candidate.get("compiler_binding")),
            "consistency_verified": True, "unaffected_canvas_groups_equal": preserved}

    for mode in ("full", "incremental", "auto"):
        print(f"Warmup {mode}", file=sys.stderr, flush=True)
        if trial(mode)["blocked"]:
            raise RuntimeError("Warmup blocked")
    rows = []
    # Balanced position across three rounds; no parallel compiler/load competition.
    orders = (("full", "incremental", "auto"), ("incremental", "auto", "full"),
              ("auto", "full", "incremental"))
    for round_index, modes in enumerate(orders, 1):
        for mode in modes:
            print(f"Round {round_index}: {mode}", file=sys.stderr, flush=True)
            calls.clear()
            response = trial(mode)
            if response["blocked"]:
                raise RuntimeError("Measured trial blocked")
            rows.append({"round": round_index, **response, "node_calls": dict(calls)})
    summaries = []
    for mode in ("full", "incremental", "auto"):
        samples = [row["total_elapsed_ms"] for row in rows if row["requested_mode"] == mode]
        summaries.append({"requested_mode": mode, "n": len(samples),
            "median_ms": round(statistics.median(samples), 3),
            "min_ms": min(samples), "max_ms": max(samples)})
    equal = all(row["artifact_sha256"] == rows[0]["artifact_sha256"] for row in rows)
    git_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
        capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    return {"measured_at_utc": datetime.now(timezone.utc).isoformat(), "write_head": git_head,
        "source_state": "working tree includes earlier uncommitted workflow improvements",
        "scope": "synthetic compiler bridge only; no service/database/model/network/UI/business execution",
        "fixture": {"js_groups": 6, "nodes_per_group": 1, "changed_groups": 1, "changed_nodes": 1},
        "method": {"warmups_per_mode": 1, "measured_per_mode": 3,
            "order": orders, "independent_fresh_node_verification_each_trial": True,
            "baseline_artifacts_provided_to_all_modes": True, "csharp_nonblank": False,
            "percentiles": "median and range only; too few samples for p95"},
        "all_modes_artifact_hashes_equal_in_this_fixture": equal,
        "summary": summaries, "samples": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.frontend.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
