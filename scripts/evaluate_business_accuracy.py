"""Offline fixed-corpus verification. Never reads business databases or opens UI."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "mcp"), str(ROOT)]

from gxp_editor.hashing import sha256_json, sha256_text
from gxp_editor.logic_review import LogicReviewError, build_business_review, validate_business_review
from gxp_editor.logic_scenarios import LogicScenarioRunner
from tests.business_accuracy_cases import (
    BUSINESS_TEMPLATES, CORPUS_VERSION, UNSUPPORTED_CASES, attribution_cases, case, group_source,
)

BINDING = {"compiler_binding_sha256": "a" * 64}  # Synthetic review identity, not production evidence.


def review_for(source, scenarios, *, targets=None, configuration=False):
    canvas = {"actionData": [{"key": key, "actionType": language, "data": ([{
        "key": "assign", "elementKey": "SetVariable", "paramsValue": {"inputParams": {"value": "fixed"}}}] if key == "main" else [])}
        for key, language in (("main", "js"), ("other", "js"), ("callback", "js"), ("backend", "csharp"))],
        "globalVariables": []}
    design = {"data": json.dumps(canvas), "js_code": source, "csharp_code": "", "relation_data": {}}
    spec = {"schema_version": 1, "revision": "fixed-corpus-v1", "scope_policy": "manual_review",
            "requirements": [{"id": "business-rule", "text": "Fixed fixture requirement",
                "source_text": "Synthetic fixed corpus, independent of candidate observations",
                "targets": [{"group_key": "main"}] if targets is None else targets}], "scenarios": scenarios}
    if configuration:
        spec["canvas_assertions"] = [{"id": "fixed-value", "requirement_ids": ["business-rule"],
            "target": {"group_key": "main", "node_key": "assign", "element_key": "SetVariable"}, "path": "/inputParams/value",
            "operator": "equals", "value": "fixed"}]
    return design, spec


def build_review(source, scenarios, report, *, targets=None, configuration=False):
    design, spec = review_for(source, scenarios, targets=targets, configuration=configuration)
    review = build_business_review(review_spec=spec, baseline_design=design, candidate_design=design,
                                   compiler_binding=BINDING, scenario_report=report)
    return review, design


def evaluate_runner(frontend, *, progress=lambda message: None):
    runner = LogicScenarioRunner(SimpleNamespace(frontend_repo=str(frontend)))
    rows, unsupported, attribution = [], [], []
    for fixture in BUSINESS_TEMPLATES:
        baseline_source = fixture["versions"][0]["source"]
        for version in fixture["versions"]:
            report = runner.run_pair(baseline_source, version["source"], fixture["scenarios"])
            states = Counter(row["candidate"]["status"] for row in report["cases"])
            status = "failed" if states["failed"] else "unverified" if states["unverified"] else "passed"
            # Use matching baseline JS so source/hash validation is exercised by
            # the real receipt as well as the runner's own gate.
            design, spec = review_for(version["source"], fixture["scenarios"])
            baseline_design = {**design, "js_code": baseline_source}
            review = build_business_review(review_spec=spec, baseline_design=baseline_design,
                candidate_design=design, compiler_binding=BINDING, scenario_report=report)
            row = {"template": fixture["id"], "version": version["id"], "expected_status": version["expected_status"],
                   "status": status, "case_counts": dict(states), "source_sha256": sha256_text(version["source"]),
                   "spec_sha256": report["spec_sha256"], "review_status": review["status"],
                   "gate_blocked": review["candidate_gate"]["blocked"],
                   "accepted": status == version["expected_status"] and not states["unverified"],
                   "browser_verified": report["browser_verified"], "runtime_verified": report["runtime_verified"]}
            rows.append(row)
        progress("Runner corpus: " + fixture["id"])
    unsupported_spec = {"schema_version": 1, "cases": [case("unsupported", {"result": "before"}, {"result": "after"})]}
    for fixture in UNSUPPORTED_CASES:
        report = runner.run_pair(fixture["source"], fixture["source"], unsupported_spec)
        observed = report["cases"][0]["candidate"]
        review, _ = build_review(fixture["source"], unsupported_spec, report)
        unsupported.append({"id": fixture["id"], "status": observed["status"], "reason": observed.get("reason"),
                            "gate_blocked": review["candidate_gate"]["blocked"],
                            "accepted": observed["status"] == "unverified" and not report["business_scenarios_verified"]})
    for fixture in attribution_cases():
        scenarios = {"schema_version": 1, "cases": [fixture["case"]]}
        report = runner.run_pair(fixture["source"], fixture["source"], scenarios)
        review, _ = build_review(fixture["source"], scenarios, report, targets=fixture["targets"],
                                 configuration=fixture["id"] == "configuration-only")
        status = review["requirements"][0]["status"]
        attribution.append({"id": fixture["id"], "expected_status": fixture["expected_status"],
                            "status": status, "scenario_status": report["cases"][0]["candidate"]["status"],
                            "configuration_status": review["requirements"][0].get("configuration_status"),
                            "gate_blocked": review["candidate_gate"]["blocked"], "accepted": status == fixture["expected_status"]})
    progress("Runner corpus: unsupported and 12 attribution cases complete")
    summary = {"templates": len(BUSINESS_TEMPLATES), "versions": len(rows),
               "correct_versions": sum(row["expected_status"] == "passed" for row in rows),
               "mutated_versions": sum(row["expected_status"] == "failed" for row in rows),
               "correct_version_false_block": sum(row["expected_status"] == "passed" and row["gate_blocked"] for row in rows),
               "mutated_version_failed": sum(row["expected_status"] == "failed" and row["status"] == "failed" for row in rows),
               "mutated_version_false_green": sum(row["expected_status"] == "failed" and row["status"] == "passed" for row in rows),
               "supported_unverified_versions": sum(row["status"] == "unverified" for row in rows),
               "unsupported_unverified": sum(row["status"] == "unverified" for row in unsupported),
               "attribution_accepted": sum(row["accepted"] for row in attribution)}
    return {"summary": summary, "versions": rows, "unsupported": unsupported, "attribution": attribution}


def evaluate_freshness(frontend):
    source = group_source('return {answer:"fixed"};')
    scenarios = {"schema_version": 1, "cases": [case("binding", {}, {}, expect={"last_return": {"answer": "fixed"}})]}
    report = LogicScenarioRunner(SimpleNamespace(frontend_repo=str(frontend))).run_pair(source, source, scenarios)
    original, design = build_review(source, scenarios, report)
    rows = []
    for field, mutation in (
        ("candidate-canvas", lambda d: d.update(data=d["data"].replace('"fixed"', '"changed"'))),
        ("candidate-js", lambda d: d.update(js_code=d["js_code"] + " ")),
        ("candidate-relations", lambda d: d.update(relation_data={"changed": True})),
        ("baseline-canvas", lambda d: d.update(data=d["data"].replace('"fixed"', '"changed"'))),
        ("baseline-js", lambda d: d.update(js_code=d["js_code"] + " ")),
    ):
        changed = deepcopy(design); mutation(changed)
        kwargs = {"baseline_design": changed if field.startswith("baseline") else design,
                  "candidate_design": design if field.startswith("baseline") else changed, "compiler_binding": BINDING}
        rejected = False
        try:
            validate_business_review(original, **kwargs)
        except LogicReviewError:
            rejected = True
        rows.append({"id": field, "rejected": rejected})
    for identifier in ("compiler-binding", "fixed-expectation", "fixed-target", "receipt-assertions"):
        review = deepcopy(original); binding = deepcopy(BINDING)
        if identifier == "compiler-binding":
            binding["compiler_binding_sha256"] = "b" * 64
        elif identifier == "fixed-expectation":
            review["spec"]["scenarios"]["cases"][0]["expect"]["last_return"] = {"answer": "changed"}
        elif identifier == "fixed-target":
            review["spec"]["requirements"][0]["targets"] = [{"group_key": "other"}]
        else:
            review["scenarios"]["cases"][0]["candidate"].pop("assertions")
        review["review_sha256"] = sha256_json({key: value for key, value in review.items() if key != "review_sha256"})
        rejected = False
        try:
            validate_business_review(review, baseline_design=design, candidate_design=design, compiler_binding=binding)
        except LogicReviewError:
            rejected = True
        rows.append({"id": identifier, "rejected": rejected})
    return {"checks": rows, "rejected": sum(row["rejected"] for row in rows), "total": len(rows)}


def evaluate_native(frontend, *, progress=lambda message: None):
    from gxp_editor.compiler import CompilerBridge, CompilerError
    from gxp_editor.config import DatabaseConfig, EditorConfig, RuntimeReferenceConfig
    config = EditorConfig(database=DatabaseConfig("127.0.0.1", 1, "unused", "unused"), frontend_repo=str(frontend),
        compiler_bridge="dist/action-design-compiler/bridge.js",
        runtime_references=RuntimeReferenceConfig(str(frontend), target_framework="net9.0"))
    bridge = CompilerBridge(config)
    runner = LogicScenarioRunner(SimpleNamespace(frontend_repo=str(frontend)))

    def value(expression):
        return {"paramTypes": "custom", "value": expression, "code": expression,
                "label": expression, "dataType": "any"}

    def variable(name, expression, group="g0"):
        return {"operation": "append_node", "action_group_key": group, "element_key": "SetVariable",
                "input_params": {"variableType": "any", "variableValue": value(expression)},
                "output_params": {"variableName": value(name)}}

    empty = {"code": "OfflineAccuracy", "actionData": [{"key": f"g{i}", "title": f"Group {i}", "actionType": "js", "data": []}
              for i in range(6)], "globalVariables": []}
    seeded = bridge._call({"command": "compile_patch", "design": empty, "operations": [variable("seed", '"safe"')]})

    def artifacts(result):
        return {key: deepcopy(result[key]) for key in ("js_code", "csharp_code", "relation_data")}

    def baseline_expression(expression):
        design = deepcopy(seeded["design"])
        design["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["variableValue"] = value(expression)
        return design, artifacts(bridge._call({"command": "compile_design", "design": design}))

    safe_design, safe_saved = baseline_expression('"safe"')
    old_design, old_saved = baseline_expression("missingContext.id")
    dead_design, dead_saved = baseline_expression('false ? missingContext.id : "safe"')
    node = dead_design["actionData"][0]["data"][0]
    become_live = {"operation": "set_parameter", "action_group_key": "g0", "target_node_key": node["key"],
        "expected_element_key": "SetVariable", "path": "/inputParams/variableValue",
        "expected_old_value": deepcopy(node["paramsValue"]["inputParams"]["variableValue"]),
        "value": value('true ? missingContext.id : "safe"')}
    dependency = bridge.compile_patch(safe_design, [{"operation": "append_node", "action_group_key": caller,
        "element_key": "CallAction", "input_params": {"actionName": {"value": callee, "label": callee, "actionType": "js"}}}
        for caller, callee in (("g1", "g0"), ("g2", "g1"))])
    seeds = [
        ("supported-write", safe_design, safe_saved, [variable("effect", '(inbiz("result").value="after")')], "accepted", "g0"),
        ("old-reference-retained", old_design, old_saved, [variable("changed", '"safe"', "g5")], "accepted-with-old-diagnostic", None),
        ("duplicate-old-reference", old_design, old_saved, [variable("duplicate", "missingContext.id")], "rejected", None),
        ("old-reference-moved-group", old_design, old_saved, [variable("moved", "missingContext.id", "g5")], "rejected", None),
        ("dead-reference-now-live", dead_design, dead_saved, [become_live], "rejected", None),
        ("transitive-callers", dependency["design"], artifacts(dependency), [variable("effect", '(inbiz("result").value="after")')], "accepted", "g2"),
    ]
    rows = []
    for identifier, design, saved, operations, expected, call_group in seeds:
        for mode in ("full", "incremental", "auto"):
            row = {"seed": identifier, "mode": mode, "expected": expected, "accepted": False}
            try:
                candidate = bridge.compile_patch(design, operations, baseline_design=design,
                    baseline_artifacts=saved, generation_mode=mode)
                evidence = bridge.verify_compiled_result(candidate, baseline_design=design,
                    baseline_artifacts=saved, generation_mode=mode)
                row.update(status="accepted", selected_mode=candidate["generation_selection"]["selected_mode"],
                           independently_verified=evidence["generated_code_consistency_verified"],
                           baseline_diagnostics=len(candidate["event_binding_validation"].get("baseline_diagnostics", [])))
                if call_group:
                    scenarios = {"schema_version": 1, "cases": [case("native-effect", {"result": "before"}, {"result": "after"},
                        steps=[{"kind": "call_group", "group_key": call_group}])]}
                    observed = runner.run_pair(saved["js_code"], candidate["js_code"], scenarios)
                    row["runner_status"] = observed["cases"][0]["candidate"]["status"]
                else:
                    scenarios = {"schema_version": 1, "cases": [case("old-global", {}, {},
                        steps=[{"kind": "call_group", "group_key": "g5"}], expect={"last_return": {}})]}
                    observed = runner.run_pair(saved["js_code"], candidate["js_code"], scenarios)
                    row["runner_status"] = observed["cases"][0]["candidate"]["status"]
                row["accepted"] = (expected != "rejected" and row["independently_verified"]
                    and (row["runner_status"] == "passed" if call_group else
                         row["baseline_diagnostics"] > 0 and row["runner_status"] == "unverified"))
            except CompilerError as error:
                details = error.details
                row.update(status="rejected", rejection_phase=details.get("phase"),
                           rejection_reason=details.get("reason_code"), identifier=details.get("identifier"),
                           accepted=(expected == "rejected" and details.get("reason_code") == "undeclared_identifier"
                                     and details.get("identifier") == "missingContext"))
            rows.append(row)
            progress("Native corpus: " + identifier + " / " + mode + " -> " + row["status"])
    return {"seeds": len(seeds), "checks": rows, "accepted": sum(row["accepted"] for row in rows), "total": len(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native", action="store_true", help="Also execute 6 seeds x 3 native compile/verify modes")
    args = parser.parse_args()
    progress = lambda message: print(message, file=sys.stderr, flush=True)
    result = {"corpus_version": CORPUS_VERSION, "measured_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "synthetic offline execution and internal evidence checks; no DB, UI, publication or model accuracy measurement",
              "runner_limits": {"max_cases_per_suite": 20, "max_suite_seconds": 20},
              "fixture_sha256": sha256_text((ROOT / "tests/business_accuracy_cases.py").read_text(encoding="utf-8"))}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint():
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result["runner"] = evaluate_runner(args.frontend, progress=progress)
    checkpoint()
    result["freshness"] = evaluate_freshness(args.frontend)
    checkpoint()
    if args.native:
        result["native"] = evaluate_native(args.frontend, progress=progress)
        checkpoint()
    checks = ([row["accepted"] for section in ("versions", "unsupported", "attribution") for row in result["runner"][section]]
              + [row["rejected"] for row in result["freshness"]["checks"]]
              + [row["accepted"] for row in result.get("native", {}).get("checks", [])])
    result["all_checks_passed"] = all(checks)
    checkpoint()
    print(json.dumps({"runner": result["runner"]["summary"], "freshness": result["freshness"],
                      "native": {key: value for key, value in result.get("native", {}).items() if key != "checks"},
                      "all_checks_passed": result["all_checks_passed"]}, ensure_ascii=False), flush=True)
    return 0 if result["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
