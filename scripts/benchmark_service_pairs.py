"""Alternate isolated baseline/candidate service runs on the same machine."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--sizes", type=int, nargs="+", default=[6, 1000])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 5:
        parser.error("at least five paired rounds required")
    results, samples, hashes, evidence_hashes, response_hashes = [], [], {}, {}, {}
    script = Path(__file__).with_name("benchmark_service_pipeline.py")
    with tempfile.TemporaryDirectory(prefix="gxp-service-pairs-") as temporary:
        for repeat in range(args.rounds):
            order = ("baseline", "candidate") if repeat % 2 == 0 else ("candidate", "baseline")
            for label in order:
                print(f"pair={repeat + 1} implementation={label}", flush=True)
                output = Path(temporary) / f"{repeat}-{label}.json"
                command = [sys.executable, "-B", str(script), "--implementation-root", str(getattr(args, label).resolve()),
                           "--frontend", str(args.frontend.resolve()), "--rounds", "1", "--sizes",
                           *map(str, args.sizes), "--output", str(output)]
                # The child has bounded native calls; bound its entire service chain too.
                completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=600)
                if completed.returncode:
                    raise RuntimeError(f"{label} round {repeat + 1} failed: {completed.stderr[-4000:]}")
                report = json.loads(output.read_text(encoding="utf-8"))
                if not report["candidate_hashes_stable"]:
                    raise RuntimeError("unstable candidate")
                if hashes and report["candidate_hashes"] != hashes:
                    raise RuntimeError("baseline/candidate generated artifacts differ")
                hashes = report["candidate_hashes"]
                if not report["evidence_hashes_stable"] or evidence_hashes and report["evidence_hashes"] != evidence_hashes:
                    raise RuntimeError("baseline/candidate complete plan or approval evidence differs")
                evidence_hashes = report["evidence_hashes"]
                for row in report["samples"]:
                    identity = (row["nodes"], row["tool"], row.get("page"))
                    if identity in response_hashes and response_hashes[identity] != row["response_sha256"]:
                        raise RuntimeError(f"baseline/candidate public output differs: {identity}")
                    response_hashes[identity] = row["response_sha256"]
                samples.extend({**row, "implementation": label, "pair": repeat + 1} for row in report["samples"])
                results.append({"pair": repeat + 1, "implementation": label, "summary": report["summary"]})
    summary = []
    for label in ("baseline", "candidate"):
        keys = sorted({(row["nodes"], row["initial_cache"], row["tool"], row.get("page") or "") for row in samples})
        for size, cache, tool, page in keys:
            rows = [row for row in samples if row["implementation"] == label and row["nodes"] == size
                    and row["initial_cache"] == cache and row["tool"] == tool and (row.get("page") or "") == page]
            summary.append({"implementation": label, "nodes": size, "initial_cache": cache, "tool": tool, "page": page or None,
                "n": len(rows), "median_ms": round(statistics.median(row["total_elapsed_ms"] for row in rows), 3),
                "min_ms": min(row["total_elapsed_ms"] for row in rows), "max_ms": max(row["total_elapsed_ms"] for row in rows),
                "median_source_scans": statistics.median(row["counts"].get("source_scans", 0) for row in rows)})
    chains, acceptance = [], []
    for size in args.sizes:
        for cache in ("cold", "warm"):
            medians = {}
            for label in ("baseline", "candidate"):
                elapsed = []
                for pair in range(1, args.rounds + 1):
                    trial = [row for row in samples if row["implementation"] == label and row["nodes"] == size
                             and row["initial_cache"] == cache and row["pair"] == pair]
                    if len(trial) != 6:
                        raise RuntimeError("Expected six requests per complete service chain")
                    elapsed.append(sum(row["total_elapsed_ms"] for row in trial))
                medians[label] = statistics.median(elapsed)
                chains.append({"implementation": label, "nodes": size, "initial_cache": cache,
                    "n": len(elapsed), "median_ms": round(medians[label], 3),
                    "min_ms": round(min(elapsed), 3), "max_ms": round(max(elapsed), 3),
                    "samples_ms": [round(value, 3) for value in elapsed]})
            change = 100 * (medians["candidate"] / medians["baseline"] - 1)
            acceptance.append({"nodes": size, "initial_cache": cache, "median_change_percent": round(change, 3),
                "criterion": "regression_at_most_5_percent" if cache == "cold" else "median_must_decrease",
                "accepted": change <= 5 if cache == "cold" else change < 0})
    result = {"scope": "five paired offline service runs; alternating whole baseline/candidate subprocesses on one machine",
        "baseline_root": str(args.baseline.resolve()), "candidate_root": str(args.candidate.resolve()),
        "rounds": args.rounds, "candidate_hashes_match": True, "candidate_hashes": hashes,
        "complete_public_outputs_match": True, "complete_plan_and_approval_evidence_match": True,
        "evidence_hashes": evidence_hashes,
        "determinism": "synthetic Python UUIDs and transaction timestamps fixed; public response hashes exclude only telemetry fields",
        "cache_semantics": "cold clears registry/contract caches before begin; remaining requests are warm; OS cache not reset",
        "limitations": ["synthetic repository; real local compiler; blank C#", "no model/network/real database/UI measurements",
                        "five observations do not establish p95", "background machine load is not controlled"],
        "summary": summary, "complete_chains": chains,
        "performance_acceptance": {"accepted": all(row["accepted"] for row in acceptance), "comparisons": acceptance},
        "samples": samples}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if not result["performance_acceptance"]["accepted"]:
        raise RuntimeError("Performance acceptance failed; retain raw measurements and revert the optimization")


if __name__ == "__main__":
    main()
