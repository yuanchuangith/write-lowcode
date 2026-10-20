"""Read-only timings for saved canvases and optional local native generation.

Only timing/count metadata is written. Candidate code is generated, never run.
No editor configuration or database credentials are loaded.
"""
from __future__ import annotations

import argparse
import cProfile
import json
import os
import pstats
import sqlite3
import statistics
import sys
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))

from gxp_editor.approval_display import canvas_change_summary
from gxp_editor.code_review_model import aligned_code_rows
from gxp_editor.component_preview import component_changes, control_flow_impacts
from gxp_editor.generated_code_review import canvas_context
from gxp_editor.hashing import sha256_json
from gxp_editor.logic_flow_model import flow_comparison, flow_options
from gxp_editor.logic_review import build_business_review, validate_business_review
from gxp_editor.logic_review_model import change_display_context, review_display_context


def read_json(path):
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Input must be a JSON object")
    return value


def canvas_and_design(value):
    if isinstance(value.get("data"), str):
        canvas = json.loads(value["data"])
    elif isinstance(value.get("data"), dict):
        canvas = value["data"]
    elif isinstance(value.get("data_json"), dict):
        canvas = value["data_json"]
    else:
        canvas = value
    if not isinstance(canvas, dict) or not isinstance(canvas.get("actionData"), list):
        raise ValueError("Canvas must contain an actionData array")
    if not isinstance(canvas.get("globalVariables"), list):
        raise ValueError("Canvas must contain a globalVariables array")
    design = dict(value)
    design.setdefault("data", json.dumps(canvas, ensure_ascii=False, separators=(",", ":")))
    if not isinstance(design["data"], str):
        design["data"] = json.dumps(canvas, ensure_ascii=False, separators=(",", ":"))
    return canvas, design


def load_inputs(args):
    if not args.artifact:
        if not args.before or not args.after:
            raise ValueError("Provide both --before and --after, or --artifact")
        return read_json(args.before), read_json(args.after), None
    if args.before or args.after:
        raise ValueError("--artifact cannot be combined with --before or --after")
    artifact = read_json(args.artifact)
    candidate = artifact.get("desired_design")
    if not isinstance(candidate, dict):
        raise ValueError("Artifact must contain desired_design")
    if args.baseline:
        baseline = read_json(args.baseline)
    elif isinstance(artifact.get("baseline"), dict):
        baseline = artifact["baseline"]
    else:
        state_path = args.state_db
        if state_path is None and os.environ.get("LOCALAPPDATA"):
            state_path = Path(os.environ["LOCALAPPDATA"]) / "GxpLowcodeEditor/state/editor.sqlite3"
        if state_path is None or not state_path.is_file():
            raise ValueError("Artifact baseline unavailable; supply --baseline or --state-db")
        with sqlite3.connect(state_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT baseline_json FROM sessions WHERE id=?", (artifact.get("session_id"),)).fetchone()
        if row is None:
            raise ValueError("Artifact session has no local baseline; supply --baseline")
        baseline = json.loads(row[0])
    return baseline, candidate, artifact


def model_benchmark(before, after, baseline, candidate, artifact, repeats):
    direct = component_changes(before, after)
    report = (artifact or {}).get("business_review") or {"changes": [
        {"group_key": side.get("group_key"), "node_key": side.get("node", {}).get("key")}
        for item in direct if (side := item.get("after") or item.get("before"))]}
    targets = flow_options(before, after, report)
    context = canvas_context(before, after)
    display_context = review_display_context(before, after)
    jobs = {
        "parse_two_canvases": lambda: (json.loads(baseline["data"]), json.loads(candidate["data"])),
        "component_changes": lambda: component_changes(before, after),
        "control_flow_impacts": lambda: control_flow_impacts(before, after),
        "control_flow_impacts_reuse_changes": lambda: control_flow_impacts(before, after, changes=direct),
        "flow_options": lambda: flow_options(before, after, report),
        "flow_comparison_changed_targets": lambda: [flow_comparison(before, after, target) for target in targets],
        "canvas_context": lambda: canvas_context(before, after),
        "review_display_context": lambda: review_display_context(before, after),
        "change_display_context_reuse": lambda: [change_display_context(change, before, after, context=display_context)
                                                 for change in report["changes"]],
        "canvas_change_summary_reuse": lambda: canvas_change_summary(before, after, changes=direct),
    }
    for language, field in (("js", "js_code"), ("csharp", "csharp_code")):
        if isinstance(baseline.get(field), str) and isinstance(candidate.get(field), str):
            jobs["aligned_code_rows_" + language] = lambda field=field: aligned_code_rows(
                baseline[field], candidate[field], context=context)
    if artifact and artifact.get("business_review"):
        binding = (artifact.get("compiler_evidence") or {}).get("compiler_binding") or {}
        hashes = (artifact.get("compiler_evidence") or {}).get("hashes")
        jobs["validate_business_review"] = lambda: validate_business_review(report, baseline_design=baseline,
            candidate_hashes=hashes, compiler_binding=binding)
        build_candidate = dict(candidate)
        if "relation_data" not in build_candidate and (hashes or {}).get("relation_sha256") == sha256_json({}):
            build_candidate["relation_data"] = {}
        if "relation_data" in build_candidate:
            jobs["build_business_review"] = lambda: build_business_review(review_spec=report["spec"],
                baseline_design=baseline, candidate_design=build_candidate,
                compiler_binding=binding, scenario_report=report["scenarios"])
    timings = {}
    # Reserve one execution for cProfile; instrumented calls are not included in
    # the wall-clock samples, and no model runs more than --repeats times.
    sample_repeats = max(1, repeats - 1)
    for name, call in jobs.items():
        print("Measuring " + name, file=sys.stderr, flush=True)
        samples = []
        for _ in range(sample_repeats):
            started = perf_counter()
            call()
            samples.append(round((perf_counter() - started) * 1000, 3))
        timings[name] = {"repetitions": sample_repeats, "median_ms": statistics.median(samples), "samples_ms": samples}
    profiler = cProfile.Profile()
    if repeats > 1:
        profiler.enable()
        for call in jobs.values():
            call()
        profiler.disable()
    rows = []
    stats = pstats.Stats(profiler).stats if repeats > 1 else {}
    for (file, line, function), (_, calls, own, cumulative, callers) in stats.items():
        if "gxp_editor" not in Path(file).parts:
            continue
        rows.append({"file": Path(file).name, "line": line, "function": function, "calls": calls,
            "own_ms": round(own * 1000, 3), "cumulative_ms": round(cumulative * 1000, 3),
            "callers": [{"file": Path(key[0]).name, "line": key[1], "function": key[2], "calls": value[0]}
                        for key, value in callers.items()]})
    return {"counts": {"before_groups": len(before["actionData"]), "after_groups": len(after["actionData"]),
        "before_nodes": sum(len(g.get("data", [])) for g in before["actionData"]),
        "after_nodes": sum(len(g.get("data", [])) for g in after["actionData"]),
        "changed_components": len(direct), "flow_targets": len(targets)},
        "timings": timings, "profile_top_cumulative": sorted(rows, key=lambda row: row["cumulative_ms"], reverse=True)[:10]}


def native_benchmark(args, canvas, baseline, candidate):
    # Construct dummy values directly: never load_config(), keyring, or a writer.
    from gxp_editor.compiler import CompilerBridge
    from gxp_editor.config import DatabaseConfig, EditorConfig, RuntimeReferenceConfig

    class BoundedBridge(CompilerBridge):
        def _call(self, payload, *, timeout_seconds=25):
            return super()._call(payload, timeout_seconds=min(timeout_seconds, 25))

    frontend = args.frontend.resolve()
    bridge = BoundedBridge(EditorConfig(
        database=DatabaseConfig(host="127.0.0.1", port=1, database="offline_unused", user="offline_unused"),
        frontend_repo=str(frontend), compiler_bridge=args.bridge,
        runtime_references=RuntimeReferenceConfig(directory=str(frontend))))
    design = deepcopy(canvas)
    metadata = {**baseline, **candidate}
    design.update({"refId": str(metadata.get("ref_id") or metadata.get("refId") or "offline-profile"),
        "id": str(metadata.get("id") or "offline-profile"), "name": str(metadata.get("name") or "Offline profile"),
        "code": args.action_code or str(metadata.get("code") or ""),
        "describe": str(metadata.get("describe") or ""), "isPublish": False,
        "version": int(metadata.get("version") or 0)})
    if not design["code"]:
        raise ValueError("Native generation needs action metadata; supply --action-code for raw canvases")
    preferred = {"SelectData", "QueryData", "SetVariable", "SetVariableValue"}
    eligible = [(g, n) for g in design["actionData"] for n in g.get("data", [])
                if n.get("elementKey") in preferred and isinstance(n.get("paramsValue"), dict)]
    eligible.sort(key=lambda pair: pair[1].get("elementKey") != "SelectData")
    if not eligible:
        raise ValueError("Native benchmark needs a supported parameterized node for a same-parameters patch")
    group, node = eligible[0]
    operation = {"operation": "replace_params", "action_group_key": group["key"], "target_node_key": node["key"],
        "input_params": deepcopy(node["paramsValue"].get("inputParams") or {}),
        "output_params": deepcopy(node["paramsValue"].get("outputParams") or {})}
    counters = defaultdict(lambda: {"calls": 0, "elapsed_ms": 0.0})
    for name in ("_verify_native_sources", "_git", "_call", "describe_registry"):
        original = getattr(bridge, name)
        def wrapped(*arguments, _name=name, _original=original, **keywords):
            started = perf_counter()
            try:
                return _original(*arguments, **keywords)
            finally:
                counters[_name]["calls"] += 1
                counters[_name]["elapsed_ms"] += (perf_counter() - started) * 1000
        setattr(bridge, name, wrapped)
    timings = {}
    def measure(name, call):
        print("Measuring native " + name, file=sys.stderr, flush=True)
        counters.clear()
        started = perf_counter()
        value = call()
        timings[name] = {"repetitions": 1, "elapsed_ms": round((perf_counter() - started) * 1000, 3),
            "calls": {key: {"calls": value["calls"], "elapsed_ms": round(value["elapsed_ms"], 3)}
                      for key, value in counters.items()}}
        return value
    measure("native_manifest_check", bridge._verify_native_sources)
    registry = measure("describe_registry_cold", bridge.describe_registry)
    measure("describe_registry_cached", bridge.describe_registry)
    compiled = measure("compile_patch_same_parameters", lambda: bridge.compile_patch(design, [operation]))
    evidence = measure("verify_compiled_result", lambda: bridge.verify_compiled_result(compiled))
    return {"timings": timings, "registry_elements": len(registry["elements"]),
        "validation": {"valid": compiled["validation"].get("valid") is True,
            "code_generation_complete": evidence["code_generation_complete"],
            "generated_code_consistency_verified": evidence["generated_code_consistency_verified"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, help="Before canvas or design JSON")
    parser.add_argument("--after", type=Path, help="After canvas or design JSON")
    parser.add_argument("--artifact", type=Path, help="Saved approval artifact JSON")
    parser.add_argument("--baseline", type=Path, help="Baseline JSON for artifact mode")
    parser.add_argument("--state-db", type=Path, help="Read-only local state SQLite for artifact baseline lookup")
    parser.add_argument("--frontend", type=Path, help="Opt in to generation with this local frontend repository")
    parser.add_argument("--bridge", default="dist/action-design-compiler/bridge.js", help="Native bridge relative to frontend")
    parser.add_argument("--action-code", help="Action identifier required by native generation of raw canvases")
    parser.add_argument("--repeats", type=int, choices=(1, 2, 3), default=3,
        help="Maximum model executions, including one separate cProfile pass when greater than 1")
    parser.add_argument("--output", type=Path, help="Write metadata-only JSON report here")
    args = parser.parse_args()
    try:
        before_value, after_value, artifact = load_inputs(args)
        before, baseline = canvas_and_design(before_value)
        after, candidate = canvas_and_design(after_value)
        result = {"scope": "read-only input models; no business database connection or candidate code execution",
            "models": model_benchmark(before, after, baseline, candidate, artifact, args.repeats)}
        if args.frontend:
            result["native"] = native_benchmark(args, after, baseline, candidate)
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
    except Exception as exc:
        # Compiler diagnostics can contain business expressions; do not echo them.
        print(f"Benchmark stopped ({type(exc).__name__}). Check input shape and verified bridge sources.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
