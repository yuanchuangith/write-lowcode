"""Freeze blinded skill-only snapshots; never open private scoring expectations."""
from __future__ import annotations

import hashlib
import argparse
import json
import subprocess
from pathlib import Path

WRITE = Path(__file__).resolve().parents[1]
LOOK = WRITE.parent / "look"
BASELINES = {"write": "c475db578d283b7c9e78487154fed1346a223fd9",
             "look": "4986e2c3b71e74911aa6671a0061863e3c3b15d3"}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-only", action="store_true")
    parser.add_argument("--candidate-label", default="B")
    args = parser.parse_args()
    if len(args.candidate_label) != 1 or not args.candidate_label.isalpha() or not args.candidate_label.isupper() or args.candidate_label == "A":
        parser.error("candidate-label must be a single uppercase letter other than A")
    destination = WRITE / "output" / "routing-evaluation-20261027"
    reports = WRITE / "docs" / "evaluation-2026-10-27"
    metadata_path = reports / (f"snapshots-{args.candidate_label}.json" if args.candidate_only else "snapshots.json")
    conditions = [(args.candidate_label, "candidate")] if args.candidate_only else [("A", "baseline"), (args.candidate_label, "candidate")]
    if metadata_path.exists() or any((destination / condition).exists() for condition, _ in conditions):
        raise FileExistsError("Frozen snapshots already exist; choose a new candidate-only label")
    inputs = (reports / "corpus-inputs.json").read_bytes()
    public = json.loads(inputs)
    assert len(public["cases"]) == 30
    assert all(set(case) == {"id", "input", "fixtures"} for case in public["cases"])
    destination.mkdir(parents=True, exist_ok=True)
    input_path = destination / "inputs.json"
    if input_path.exists() and input_path.read_bytes() != inputs:
        raise ValueError("Public inputs differ from frozen inputs")
    input_path.write_bytes(inputs)
    metadata = {"inputs_sha256": hashlib.sha256(inputs).hexdigest(), "baselines": BASELINES,
                "conditions": {}, "runtime": "all trials inherit the same parent model/settings, no overrides",
                "configured_model": "gpt-6.1-sol", "backend_revision": "not exposed by collaboration runtime",
                "trial_count_per_condition": 3, "cases_per_trial": 30,
                "protocol": "Independent fresh-context runners see only public inputs and one skill snapshot; no business tools execute."}
    for condition, revision in conditions:
        files = {}
        for name, root in (("look", LOOK), ("write", WRITE)):
            if revision == "baseline":
                paths = git(root, "ls-tree", "-r", "--name-only", BASELINES[name], "--", "skills").decode().splitlines()
                content = {p: git(root, "show", f"{BASELINES[name]}:{p}") for p in paths if p.endswith(".md")}
            else:
                content = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "skills").rglob("*.md")}
            for relative, data in content.items():
                target = destination / condition / name / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                files[f"{name}/{relative}"] = hashlib.sha256(data).hexdigest()
        metadata["conditions"][condition] = {"revision": revision, "files": files,
            "manifest_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"directory": str(destination), "cases": 30, "conditions": list(metadata["conditions"])}))


if __name__ == "__main__":
    main()
