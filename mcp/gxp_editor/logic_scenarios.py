"""Bounded, offline execution of generated JavaScript against explicit expectations."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from .hashing import sha256_json, sha256_text


class LogicScenarioSpecError(ValueError):
    pass


RUNNER_VERSION = "1"
MAX_SPEC_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_CASES = 20
MAX_STEPS = 10
MAX_SAFE_INTEGER = 2**53 - 1
_GROUP = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,127}\Z")


def _object(value: Any, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise LogicScenarioSpecError(f"{label} must be an object with string keys")
    extra = set(value) - allowed
    if extra:
        raise LogicScenarioSpecError(f"{label} contains unsupported fields: {', '.join(sorted(extra))}")
    return value


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise LogicScenarioSpecError(f"{label} must be a non-empty string of at most 128 characters")
    return value


def _fields(value: Any, label: str, *, expected: bool = False) -> None:
    if not isinstance(value, dict) or len(value) > 100:
        raise LogicScenarioSpecError(f"{label} must contain at most 100 declared fields")
    for name, field in value.items():
        _name(name, label + " ID")
        item = _object(field, {"value"} if expected else {"value", "text", "selected_rows"}, label + "." + name)
        if expected and "value" not in item:
            raise LogicScenarioSpecError(f"{label}.{name} must declare value")
        if "selected_rows" in item and not isinstance(item["selected_rows"], list):
            raise LogicScenarioSpecError(f"{label}.{name}.selected_rows must be an array")


def validate_scenario_spec(spec: Any) -> dict[str, Any]:
    """Validate data-only fixtures; no executable stub or predicate is accepted."""
    try:
        encoded = json.dumps(spec, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise LogicScenarioSpecError("Scenario specification must contain finite JSON values") from exc
    if len(encoded.encode("utf-8")) > MAX_SPEC_BYTES:
        raise LogicScenarioSpecError("Scenario specification exceeds 1 MiB")
    pending = [spec]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
        elif type(value) is int or (type(value) is float and value.is_integer()):
            if abs(value) > MAX_SAFE_INTEGER:
                raise LogicScenarioSpecError("Integers outside JavaScript's safe range must be encoded as strings")
    spec = _object(spec, {"schema_version", "cases"}, "scenarios")
    if type(spec.get("schema_version")) is not int or spec["schema_version"] != 1:
        raise LogicScenarioSpecError("scenarios.schema_version must be 1")
    cases = spec.get("cases")
    if not isinstance(cases, list) or len(cases) > MAX_CASES:
        raise LogicScenarioSpecError("scenarios.cases must be an array of at most 20 cases")
    seen: set[str] = set()
    for case in cases:
        case = _object(case, {"id", "title", "requirement_ids", "environment", "steps", "expect"}, "scenario")
        case_id = _name(case.get("id"), "scenario.id")
        if case_id in seen:
            raise LogicScenarioSpecError("Scenario IDs must be unique")
        seen.add(case_id)
        if "requirement_ids" in case:
            links = case["requirement_ids"]
            if not isinstance(links, list) or len(links) > 100:
                raise LogicScenarioSpecError("scenario.requirement_ids must be an array of at most 100 IDs")
            for link in links:
                _name(link, "scenario requirement ID")
            if len(set(links)) != len(links):
                raise LogicScenarioSpecError("Scenario requirement IDs must be unique")
        if "title" in case and (not isinstance(case["title"], str) or len(case["title"]) > 500):
            raise LogicScenarioSpecError("Scenario title must be a string of at most 500 characters")
        environment = _object(case.get("environment", {}), {"fields", "child_fields", "page_mode"}, "environment")
        for section in ("fields", "child_fields"):
            if section in environment:
                _fields(environment[section], "environment." + section)
        if "page_mode" in environment:
            _name(environment["page_mode"], "environment.page_mode")
        steps = case.get("steps")
        if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
            raise LogicScenarioSpecError("Each scenario needs 1 to 10 steps")
        for step in steps:
            if not isinstance(step, dict):
                raise LogicScenarioSpecError("Scenario steps must be objects")
            kind = step.get("kind")
            allowed = {
                "call_group": {"kind", "group_key", "args"},
                "emit_event": {"kind", "component_id", "event_name", "args", "inject_inner_inbiz"},
                "set_inputs": {"kind", "fields", "child_fields", "page_mode"},
                "read_metadata": {"kind"},
            }
            if not isinstance(kind, str) or kind not in allowed:
                raise LogicScenarioSpecError("Unsupported scenario step kind")
            _object(step, allowed[kind], "step")
            if kind == "call_group" and not _GROUP.fullmatch(_name(step.get("group_key"), "step.group_key")):
                raise LogicScenarioSpecError("step.group_key must be a JavaScript identifier")
            if kind == "emit_event":
                _name(step.get("component_id"), "step.component_id")
                _name(step.get("event_name"), "step.event_name")
                if "inject_inner_inbiz" in step and type(step["inject_inner_inbiz"]) is not bool:
                    raise LogicScenarioSpecError("step.inject_inner_inbiz must be boolean")
            if kind in {"call_group", "emit_event"} and not isinstance(step.get("args", []), list):
                raise LogicScenarioSpecError("step.args must be an array")
            if kind == "set_inputs":
                for section in ("fields", "child_fields"):
                    if section in step:
                        _fields(step[section], "step." + section)
                if "page_mode" in step:
                    _name(step["page_mode"], "step.page_mode")
        expect = _object(case.get("expect", {}), {"fields", "metadata", "metadata_registrations", "last_return", "last_return_undefined", "call_counts", "call_order"}, "expect")
        if "fields" in expect:
            _fields(expect["fields"], "expect.fields", expected=True)
        if "metadata" in expect:
            if expect["metadata"] is not None and not isinstance(expect["metadata"], list):
                raise LogicScenarioSpecError("expect.metadata must be an array or null")
            for item in expect["metadata"] or []:
                _object(item, {"key", "value"}, "metadata entry")
                _name(item.get("key"), "metadata key")
                if "value" not in item:
                    raise LogicScenarioSpecError("Expected metadata entries must declare value")
        if "metadata_registrations" in expect and (type(expect["metadata_registrations"]) is not int or expect["metadata_registrations"] < 0):
            raise LogicScenarioSpecError("expect.metadata_registrations must be a non-negative integer")
        if "last_return_undefined" in expect:
            if expect["last_return_undefined"] is not True or "last_return" in expect:
                raise LogicScenarioSpecError("last_return_undefined must be true and cannot accompany last_return")
        if "call_counts" in expect:
            if not isinstance(expect["call_counts"], dict):
                raise LogicScenarioSpecError("expect.call_counts must be an object")
            for name, count in expect["call_counts"].items():
                _name(name, "call count name")
                if type(count) is not int or count < 0:
                    raise LogicScenarioSpecError("Expected call counts must be non-negative integers")
        if "call_order" in expect:
            if not isinstance(expect["call_order"], list) or len(expect["call_order"]) > 1000:
                raise LogicScenarioSpecError("expect.call_order must be an array of at most 1000 API names")
            for name in expect["call_order"]:
                _name(name, "call order API name")
    return deepcopy(spec)


normalize_scenario_spec = validate_scenario_spec


class LogicScenarioRunner:
    CASE_TIMEOUT_SECONDS = 2.0
    SUITE_TIMEOUT_SECONDS = 20.0
    MAX_OUTPUT_BYTES = 2 * 1024 * 1024

    def __init__(self, config=None):
        self.config = config

    @staticmethod
    def _unverified(reason: str) -> dict[str, Any]:
        return {"status": "unverified", "reason": reason, "actual": None, "trace": [], "assertions": []}

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=2, check=False,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()

    def _run(self, source: str, case: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not isinstance(source, str) or not source.strip():
            return self._unverified("No generated JavaScript is available for this version")
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            return self._unverified("Generated JavaScript exceeds the 2 MiB execution limit")
        payload = {"protocol_version": 1, "source_sha256": sha256_text(source), "js_code": source,
                   "scenario": case, "frontend_root": str(getattr(self.config, "frontend_repo", ""))}
        process = None
        try:
            environment = {name: value for name, value in os.environ.items()
                           if name.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "SYSTEMDRIVE"}}
            process = subprocess.Popen(["node", "--max-old-space-size=128", str(Path(__file__).with_name("logic_scenario_runner.cjs"))],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=environment, start_new_session=os.name != "nt",
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            stdout, stderr = process.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=timeout)
            if len(stdout) > self.MAX_OUTPUT_BYTES:
                return self._unverified("Scenario output exceeded its limit")
            response = json.loads(stdout.decode("utf-8"))
            if (process.returncode or not isinstance(response, dict) or response.get("protocol_version") != 1
                    or response.get("source_sha256") != payload["source_sha256"]):
                return self._unverified("Scenario runner returned an invalid response")
            result = response.get("result")
            if (not isinstance(result, dict) or result.get("status") not in {"passed", "failed", "unverified"}
                    or not isinstance(result.get("trace"), list) or not isinstance(result.get("assertions"), list)):
                return self._unverified("Scenario runner returned an invalid result")
            return result
        except subprocess.TimeoutExpired:
            return self._unverified("Scenario execution timed out")
        except (OSError, ValueError, UnicodeError) as exc:
            return self._unverified("Scenario runner could not complete: " + str(exc)[:300])
        finally:
            if process is not None:
                self._stop(process)
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()

    def run_pair(self, baseline_js: str, candidate_js: str, scenario_spec: dict[str, Any]) -> dict[str, Any]:
        spec = validate_scenario_spec(scenario_spec)
        started = time.monotonic()
        cases = [{"id": case["id"], "title": case.get("title", case["id"]),
                  "expected": deepcopy(case.get("expect", {})),
                  **({"requirement_ids": deepcopy(case["requirement_ids"])} if "requirement_ids" in case else {})}
                 for case in spec["cases"]]
        # Candidate runs first so a broken or slow baseline cannot exhaust its verification budget.
        for version, source in (("candidate", candidate_js), ("baseline", baseline_js)):
            for row, case in zip(cases, spec["cases"]):
                remaining = self.SUITE_TIMEOUT_SECONDS - (time.monotonic() - started)
                row[version] = (self._run(source, case, min(self.CASE_TIMEOUT_SECONDS, remaining))
                                if remaining > 0 else self._unverified("Scenario suite time limit reached"))
        summary = {status: sum(row["candidate"]["status"] == status for row in cases)
                   for status in ("passed", "failed", "unverified")}
        return {"schema_version": 1, "runner_version": RUNNER_VERSION,
                "spec_sha256": sha256_json(spec), "baseline_source_sha256": sha256_text(baseline_js),
                "candidate_source_sha256": sha256_text(candidate_js), "cases": cases, "summary": summary,
                "candidate_gate": {"blocked": summary["failed"] > 0, "fail_count": summary["failed"],
                                   "unknown_count": summary["unverified"], "pass_count": summary["passed"]},
                "business_scenarios_verified": bool(cases) and summary["passed"] == len(cases),
                "verification_scope": "declared_offline_javascript_scenarios",
                "browser_verified": False, "runtime_verified": False,
                "csharp_business_logic": "not_executed"}
