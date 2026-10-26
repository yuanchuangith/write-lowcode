"""Synthetic complete-validation paging allocation/time probe, no business IO."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--implementation-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--nodes", type=int, default=10000)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.implementation_root.resolve() / "mcp"))
    from gxp_editor.node_discovery import find_baseline_nodes
    design = {"actionData": [{"key": "main", "title": "Group", "actionType": "js", "data": [
        {"key": f"n{i}", "elementKey": "Noop"} for i in range(args.nodes)]}], "globalVariables": []}
    registry = {"elements": [{"key": "Noop", "input_params": [], "output_params": []}]}
    rows = []
    for name, kwargs in (("first", {"limit": 30}), ("last", {"offset": args.nodes - 30, "limit": 30}),
                         ("groups", {"kind": "groups"})):
        samples = []
        for repeat in range(args.rounds):
            tracemalloc.start()
            started = time.perf_counter()
            result = find_baseline_nodes(design, registry, **kwargs)
            elapsed = (time.perf_counter() - started) * 1000
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            encoded = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
            samples.append({"elapsed_ms": round(elapsed, 3), "peak_bytes": peak,
                "response_sha256": hashlib.sha256(encoded).hexdigest()})
        rows.append({"scenario": name, "n": args.rounds, "samples": samples,
            "median_ms": statistics.median(row["elapsed_ms"] for row in samples),
            "median_peak_bytes": statistics.median(row["peak_bytes"] for row in samples)})
    payload = {"scope": "synthetic 10k minimal nodes; full JSON/integrity checks; tracemalloc included in timing; no database/compiler/model",
        "nodes": args.nodes, "implementation_root": str(args.implementation_root.resolve()), "results": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{key: row[key] for key in ("scenario", "median_ms", "median_peak_bytes")} for row in rows]))


if __name__ == "__main__":
    main()
