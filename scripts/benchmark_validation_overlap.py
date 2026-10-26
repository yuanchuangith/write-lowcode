"""Exploratory Node + real SDK overlap; no app DLLs, not a rollout gate pass."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--implementation-root", type=Path, required=True)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.implementation_root.resolve() / "mcp"))
    from gxp_editor.compiler import CompilerBridge
    from gxp_editor.config import DatabaseConfig, EditorConfig, RuntimeReferenceConfig
    from gxp_editor.hashing import sha256_json
    from gxp_editor.runtime_validation import RuntimeCsharpValidator
    config = EditorConfig(database=DatabaseConfig("127.0.0.1", 1, "unused", "unused"),
        frontend_repo=str(args.frontend.resolve()), compiler_bridge="dist/action-design-compiler/bridge.js",
        runtime_references=RuntimeReferenceConfig(str(args.frontend.resolve()), target_framework="net9.0"))
    design = {"actionData": [{"key": "main", "title": "Main", "actionType": "js", "data": []}],
              "globalVariables": [], "code": "OverlapProbe"}
    record = lambda value: {"paramTypes": "custom", "code": value, "value": value, "dataType": "number", "label": value}
    candidate = CompilerBridge(config).compile_patch(design, [{"operation": "append_node", "action_group_key": "main",
        "element_key": "SetVariable", "input_params": {"variableType": "number", "variableValue": record("1")},
        "output_params": {"variableName": record("answer")}}])
    source_hash = sha256_json({key: candidate[key] for key in ("js_code", "csharp_code", "relation_data")})
    rows = []
    with tempfile.TemporaryDirectory(prefix="gxp-overlap-probe-") as temporary, \
            patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
        validator = RuntimeCsharpValidator(config)
        with patch.object(validator, "_references", return_value=[]):
            validator._validate("SyntheticOverlap", "public class Synthetic { public int Value => 0; }")

            def node_job():
                evidence = CompilerBridge(config).verify_compiled_result(deepcopy(candidate))
                if not evidence.get("generated_code_consistency_verified"):
                    raise RuntimeError("Node independent verification failed")
                return True

            def csharp_job(value):
                evidence = validator._validate("SyntheticOverlap", f"public class Synthetic {{ public int Value => {value}; }}")
                if not evidence.get("success"):
                    raise RuntimeError("Synthetic C# build failed")
                return True

            with ThreadPoolExecutor(max_workers=2) as pool:
                for repeat in range(args.rounds):
                    modes = ("serial", "parallel") if repeat % 2 == 0 else ("parallel", "serial")
                    for mode in modes:
                        print(f"round={repeat + 1} mode={mode}", file=sys.stderr, flush=True)
                        started = time.perf_counter()
                        if mode == "serial":
                            node_ok, csharp_ok = node_job(), csharp_job(repeat * 2 + 1)
                        else:
                            node_future = pool.submit(node_job)
                            csharp_future = pool.submit(csharp_job, repeat * 2 + 2)
                            node_ok = node_future.result(timeout=90)
                            csharp_ok = csharp_future.result(timeout=90)
                        rows.append({"round": repeat + 1, "mode": mode,
                            "total_ms": round((time.perf_counter() - started) * 1000, 3),
                            "node_verified": node_ok, "csharp_compiled": csharp_ok,
                            "candidate_hash_unchanged": source_hash == sha256_json({key: candidate[key] for key in ("js_code", "csharp_code", "relation_data")})})
    summary = {mode: {"n": args.rounds, "median_ms": statistics.median(row["total_ms"] for row in rows if row["mode"] == mode),
        "max_ms": max(row["total_ms"] for row in rows if row["mode"] == mode)} for mode in ("serial", "parallel")}
    payload = {"scope": "real independent Node + actual net9 SDK helper with zero app DLLs; separate synthetic C# class; not full isolated production validation",
        "summary": summary, "samples": rows, "production_parallel_enabled": False,
        "rollout_gate_passed": False, "reason": "No production mixed-candidate, isolated-probe failure/cleanup, or reliable tail-latency coverage; retain serial service behavior"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
