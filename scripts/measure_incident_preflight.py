"""Measure synthetic approval-preflight orchestration; never connect to a database.

Run with the repository's dependency-equipped Python. Counts are repository-level
Mock calls, not SQL counts; durations exclude fixture construction and represent
only this synthetic process. No throughput or speedup claim is made.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import platform
import statistics
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp"))

from tests.helpers import baseline, editor_config, transaction
from gxp_editor.service import EditorService

SCENARIOS = {
    "match": "match",
    "stale_payload": "draft_baseline_changed",
    "db_unavailable": "baseline_evidence_unavailable",
    "compiler_changed": "compiler_binding_changed",
}


def fixture(scenario):
    artifact = transaction()
    draft = baseline(relations=[])
    binding = deepcopy(artifact["compiler_evidence"]["compiler_binding"])
    session = {"id": artifact["session_id"], "design_id": draft["id"], "ref_id": draft["ref_id"],
               "backup_id": "synthetic-measurement-backup", "baseline": draft,
               "compiler_hash": binding["compiler_binding_sha256"], "owner_revision": 0}
    repository, compiler = Mock(), Mock()
    repository.database.read_session.side_effect = lambda: nullcontext(object())
    repository.load_draft_by_id.return_value = deepcopy(draft)
    repository._relations.return_value = []
    repository.grant_status.return_value = {"safe": True, "grants_sha256": "c" * 64,
        "principal_verified": True, "authenticated_principal": "synthetic_measurement@localhost"}
    artifact["database_safety"]["authenticated_principal"] = "synthetic_measurement@localhost"
    compiler.compiler_binding.return_value = binding
    service = object.__new__(EditorService)
    service._conversation_id = ""
    service.state = SimpleNamespace(get_session=lambda _: deepcopy(session),
        review_spec_checkpoint=lambda *args, **kwargs: None)
    service._components = lambda: (editor_config(), object(), repository, compiler, object())
    if scenario == "stale_payload":
        repository.load_draft_by_id.return_value["js_code"] = "synthetic changed JS"
    elif scenario == "db_unavailable":
        repository.database.read_session.side_effect = TimeoutError("synthetic read timeout")
    elif scenario == "compiler_changed":
        compiler.compiler_binding.return_value = {**binding, "compiler_binding_sha256": "0" * 64}
    return service, repository, compiler, session, artifact


def measure(scenario):
    service, repository, compiler, session, artifact = fixture(scenario)
    started = time.perf_counter_ns()
    try:
        result = service._approval_preflight({"kind": "write"}, session, artifact)
        outcome = result["baseline_comparison"]["status"]
    except RuntimeError as exc:
        outcome = getattr(exc, "code", type(exc).__name__)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    if outcome != SCENARIOS[scenario]:
        raise AssertionError(f"Unexpected synthetic outcome for {scenario}: {outcome}")
    return {"elapsed_ms": round(elapsed_ms, 6), "outcome": outcome,
            "repository_calls": {"read_session": repository.database.read_session.call_count,
                                 "draft": repository.load_draft_by_id.call_count,
                                 "relation": repository._relations.call_count,
                                 "grants": repository.grant_status.call_count,
                                 "compiler": compiler.compiler_binding.call_count}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path,
        default=ROOT / "docs/editor-incident-preflight-measurements-2026-10-27.json")
    args = parser.parse_args()
    if not 5 <= args.rounds <= 100:
        parser.error("rounds must be between 5 and 100")
    report = {"schema_version": 1,
        "measured_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "scope": "synthetic_approval_preflight_orchestration",
        "python_version": platform.python_version(), "platform": platform.system(),
        "rounds_per_scenario": args.rounds,
        "limitations": ["No business database, credentials, sessions, approvals, or actual compiler source are accessed.",
            "Draft, relation, grants, and compiler bindings use synthetic fixtures and Mock objects.",
            "Call counts are repository-level entry calls, not actual SQL statements or database round trips.",
            "Durations exclude fixture construction and do not represent database, production, or end-to-end latency.",
            "This report has no baseline comparison and makes no speedup claim."],
        "scenarios": []}
    samples = {name: [] for name in SCENARIOS}
    for _ in range(args.rounds):
        for name in SCENARIOS:
            samples[name].append(measure(name))
    for name, rounds in samples.items():
        report["scenarios"].append({"name": name, "expected_outcome": SCENARIOS[name],
            "median_elapsed_ms": round(statistics.median(row["elapsed_ms"] for row in rounds), 6),
            "median_repository_calls": {key: statistics.median(row["repository_calls"][key] for row in rounds)
                for key in ("read_session", "draft", "relation", "grants", "compiler")},
            "samples": rounds})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    for row in report["scenarios"]:
        print(f"{row['name']}: {row['median_elapsed_ms']} ms; {row['median_repository_calls']}")


if __name__ == "__main__":
    main()
