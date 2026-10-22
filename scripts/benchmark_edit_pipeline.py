"""Metadata-only offline benchmark; never reads editor credentials or business data.

Uses generated synthetic canvases. Optional C# smoke measures restored workspaces
directly with zero application references, not production compatibility or approval.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from copy import deepcopy
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from gxp_editor import compiler as compiler_module
from gxp_editor.compiler import CompilerBridge
from gxp_editor.config import DatabaseConfig, EditorConfig, RuntimeReferenceConfig
from gxp_editor.hashing import sha256_text
from gxp_editor.telemetry import traced_tool


def benchmark(frontend, bridge_path, include_csharp=False):
    config = EditorConfig(database=DatabaseConfig("127.0.0.1", 1, "unused", "unused"),
        frontend_repo=str(frontend), compiler_bridge=str(bridge_path),
        runtime_references=RuntimeReferenceConfig(str(frontend), target_framework="net9.0"))
    design = {"actionData": [{"key": f"g{i}", "title": f"Group {i}", "actionType": "js", "data": []}
                              for i in range(6)], "globalVariables": [], "code": "OfflineBenchmark"}
    record = lambda value: {"paramTypes": "custom", "value": value, "code": value,
                            "label": "value", "dataType": "number"}
    initial = [{"operation": "append_node", "action_group_key": f"g{i}", "element_key": "SetVariable",
                "input_params": {"variableType": "number", "variableValue": record("1")},
                "output_params": {"variableName": {**record(f"answer{i}"), "label": f"answer{i}"}}}
               for i in range(6)]
    design = CompilerBridge(config).compile_patch(design, initial)["design"]
    operations = []
    for group in design["actionData"]:
        node = group["data"][0]
        operations.append({"operation": "set_parameter", "action_group_key": group["key"],
            "target_node_key": node["key"], "expected_element_key": "SetVariable",
            "path": "/inputParams/variableValue",
            "expected_old_value": deepcopy(node["paramsValue"]["inputParams"]["variableValue"]),
            "value": record("2")})

    calls = Counter()
    class MeasuredBridge(CompilerBridge):
        def _call(self, payload, **kwargs):
            calls[payload["command"]] += 1
            return super()._call(payload, **kwargs)

    @traced_tool
    def run(batch):
        # Each request creates a bridge; independent verification remains fresh Node.
        engine = MeasuredBridge(config)
        result = engine.compile_patch(design, batch)
        evidence = engine.verify_compiled_result(result)
        return {"blocked": False, "js_sha256": sha256_text(result["js_code"]),
                "consistency_verified": evidence["generated_code_consistency_verified"]}

    rows = []
    def measure(name, batch, cold=False):
        print("Measuring " + name, file=sys.stderr, flush=True)
        if cold:
            with compiler_module._CACHE_LOCK:
                compiler_module._REGISTRY_CACHE.clear()
                compiler_module._CONTRACT_CACHE.clear()
        calls.clear()
        response = run(batch)
        # Never persist diagnostic text, operations, parameters, or generated code.
        rows.append({"scenario": name, "operation_count": len(batch), "blocked": response["blocked"],
                     "node_calls": dict(calls), "trace_id": response["trace_id"],
                     "total_elapsed_ms": response["total_elapsed_ms"], "timings": response["timings"],
                     **({"js_sha256": response["js_sha256"]} if "js_sha256" in response else {})})
        return response

    for name, batch in (("small", operations[:1]), ("broad", operations)):
        cold = measure(name + "_cold", batch, True)
        warm = measure(name + "_warm", batch)
        if cold["blocked"] or warm["blocked"] or cold["js_sha256"] != warm["js_sha256"]:
            raise RuntimeError("Cold/warm synthetic generation disagreed")
    invalid = deepcopy(operations[:2])
    invalid[1]["unsupported_field"] = True
    failed = measure("error_second_operation", invalid)
    if not failed["blocked"] or calls:
        raise RuntimeError("Invalid batch reached Node generation")
    if measure("error_corrected", operations[:2])["blocked"]:
        raise RuntimeError("Corrected synthetic batch did not complete")

    runtime_rows = []
    if include_csharp:
        from gxp_editor.runtime_validation import RuntimeCsharpValidator, RuntimeValidationError
        with tempfile.TemporaryDirectory(prefix="gxp-benchmark-") as temporary, \
                patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
            validator = RuntimeCsharpValidator(config)
            # Deliberately synthetic: actual dotnet, no application DLLs/probe evidence.
            with patch.object(validator, "_references", return_value=[]):
                for name, expression in (("cold", "1"), ("warm", "2"), ("invalid", ""), ("recovery", "3")):
                    print("Measuring C# " + name, file=sys.stderr, flush=True)
                    started = perf_counter()
                    code = ""
                    try:
                        validator._validate("offline-benchmark", f"public class Synthetic {{ public int Value => {expression}; }}")
                    except RuntimeValidationError as exc:
                        code = exc.code
                    if (name == "invalid" and code != "compilation_failed") or (name != "invalid" and code):
                        raise RuntimeError("Unexpected synthetic C# outcome")
                    runtime_rows.append({"scenario": name, "error_code": code,
                        "total_elapsed_ms": round((perf_counter() - started) * 1000, 3),
                        "timings": validator.last_performance})
    return {"scope": "synthetic offline generation; full mode; zero business writes",
            "unmeasured": ["model requests", "database", "network", "business checks", "UI approval"],
            "comparison": "current cold cache versus current warm cache; no pre-change timing baseline",
            "native": rows, "csharp": runtime_rows,
            "csharp_scope": "actual SDK direct helper smoke with no application references; not production compatibility"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--bridge", default="dist/action-design-compiler/bridge.js")
    parser.add_argument("--csharp", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = benchmark(args.frontend.resolve(), args.bridge, args.csharp)
    except Exception as exc:
        print(f"Benchmark stopped ({type(exc).__name__}); no diagnostic payload recorded.", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
