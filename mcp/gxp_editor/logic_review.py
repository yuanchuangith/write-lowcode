"""Pure business-review evidence and write gates; never execute candidate code."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from .canvas_assertions import CanvasAssertionError, build_canvas_checks, normalize_canvas_assertions, validate_json_value
from .hashing import canonical_json, design_content_hash, sha256_json, sha256_text


class LogicReviewError(ValueError):
    pass


REVIEW_SCHEMA_VERSION = 1
REVIEW_ENGINE_VERSION = "business-review-v1"
_STATUSES = {"passed", "failed", "unverified"}
_HASH = re.compile(r"[0-9a-f]{64}")
_MISSING = object()


def _text(value: Any, name: str, *, limit: int = 20000, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise LogicReviewError(f"{name} 须为{'可空' if empty else '非空'}文本，长度不超过 {limit}。")
    return value


def normalize_review_spec(spec: dict[str, Any] | None, *, legacy_scope: bool = False) -> dict[str, Any]:
    """New plans default to strict scope; stored pre-policy reports keep their hash.

    ``legacy_scope`` is only for validating existing evidence. A stored explicit
    policy is always enforced, including strict target requirements.
    """
    from .logic_scenarios import validate_scenario_spec
    if spec is None:
        spec = {"schema_version": 1, "revision": "unspecified", "requirements": [],
                "scenarios": {"schema_version": 1, "cases": []}}
    if not isinstance(spec, dict) or type(spec.get("schema_version")) is not int or spec.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise LogicReviewError("review_spec.schema_version 必须为 1。")
    try:
        validate_json_value(spec)
    except CanvasAssertionError as exc:
        raise LogicReviewError(str(exc)) from exc
    unknown = set(spec) - {"schema_version", "revision", "requirements", "scenarios", "change_source_text", "canvas_assertions", "scope_policy"}
    if unknown:
        raise LogicReviewError("review_spec 包含未知字段：" + ", ".join(sorted(unknown)))
    revision = _text(spec.get("revision"), "review_spec.revision", limit=128)
    requirements = spec.get("requirements")
    if not isinstance(requirements, list) or len(requirements) > 100:
        raise LogicReviewError("review_spec.requirements 须为不超过 100 条规则的数组。")
    seen = set()
    normalized = []
    for item in requirements:
        if not isinstance(item, dict) or set(item) - {"id", "text", "source_text", "targets"}:
            raise LogicReviewError("预期规则须包含 id、text、source_text 和 targets，不能包含未知字段。")
        identifier = _text(item.get("id"), "requirement.id", limit=128)
        if identifier in seen:
            raise LogicReviewError("预期规则 id 不能重复。")
        seen.add(identifier)
        targets = item.get("targets", [])
        if not isinstance(targets, list) or len(targets) > 100:
            raise LogicReviewError("requirement.targets 须为不超过 100 项的数组。")
        checked_targets = []
        for target in targets:
            if not isinstance(target, dict) or set(target) - {"group_key", "node_key", "field_path"}:
                raise LogicReviewError("规则目标只允许 group_key、node_key、field_path。")
            checked = {"group_key": _text(target.get("group_key"), "target.group_key", limit=256)}
            for field in ("node_key", "field_path"):
                if field in target:
                    checked[field] = _text(target[field], "target." + field, limit=512, empty=True)
            checked_targets.append(checked)
        normalized.append({"id": identifier, "text": _text(item.get("text"), "requirement.text"),
                           "source_text": _text(item.get("source_text"), "requirement.source_text"),
                           "targets": checked_targets})
    scope_policy = spec.get("scope_policy", None if legacy_scope else "strict")
    if "scope_policy" in spec and scope_policy not in ("strict", "manual_review"):
        raise LogicReviewError("review_spec.scope_policy 必须为 strict 或 manual_review。")
    if scope_policy == "strict" and (not normalized or any(not rule["targets"] for rule in normalized)):
        raise LogicReviewError("严格修改范围需要至少一条需求，且每条需求都必须指定 targets；请补充范围或显式选择 manual_review。")
    try:
        scenarios = validate_scenario_spec(spec.get("scenarios", {"schema_version": 1, "cases": []}))
    except ValueError as exc:
        raise LogicReviewError(str(exc)) from exc
    for case in scenarios["cases"]:
        if any(identifier not in seen for identifier in case.get("requirement_ids", [])):
            raise LogicReviewError("场景关联了不存在的预期规则。")
    result = {"schema_version": 1, "revision": revision, "requirements": normalized, "scenarios": scenarios}
    if scope_policy is not None:
        result["scope_policy"] = scope_policy
    if "canvas_assertions" in spec:
        try:
            result["canvas_assertions"] = normalize_canvas_assertions(spec["canvas_assertions"], normalized)
        except CanvasAssertionError as exc:
            raise LogicReviewError(str(exc)) from exc
    if "change_source_text" in spec:
        result["change_source_text"] = _text(spec["change_source_text"], "review_spec.change_source_text")
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 2 * 1024 * 1024:
        raise LogicReviewError("review_spec 超过 2 MiB 限制。")
    return result


def _canvas(design: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(design["data"]) if "data" in design else deepcopy(design.get("data_json", design))
    except (TypeError, ValueError) as exc:
        raise LogicReviewError("审查画布不是有效 JSON。") from exc
    if not isinstance(value, dict) or not isinstance(value.get("actionData"), list) or not isinstance(value.get("globalVariables"), list):
        raise LogicReviewError("审查画布须包含 actionData 和 globalVariables 数组。")
    return value


def _candidate_hashes(design: dict[str, Any]) -> dict[str, str]:
    result = {name + "_sha256": sha256_text(str(design.get(field) or ""))
              for name, field in (("data", "data"), ("js", "js_code"), ("csharp", "csharp_code"))}
    if "relation_data" in design:
        result["relation_sha256"] = sha256_json(design["relation_data"])
    return result


def _base_hash(design: dict[str, Any]) -> str:
    return str(design.get("content_hash") or design_content_hash(design))


def _matches(target: dict[str, Any], group_key: str, node_key: str, field_path: str) -> bool:
    path = target.get("field_path") or ""
    return (target["group_key"] == group_key and (not target.get("node_key") or target["node_key"] == node_key)
            and (not path or field_path == path or field_path.startswith(path + "/")))


def _scope_coverage(spec, actual_targets):
    requirement_ids = [rule["id"] for rule in spec["requirements"] if any(
        _matches(target, actual["group_key"], actual.get("node_key", ""), actual.get("field_path", ""))
        for target in rule["targets"] for actual in actual_targets)]
    covered = bool(any(rule["targets"] for rule in spec["requirements"])) and all(any(
        _matches(target, actual["group_key"], actual.get("node_key", ""), actual.get("field_path", ""))
        for rule in spec["requirements"] for target in rule["targets"]) for actual in actual_targets)
    return covered, requirement_ids


def _canvas_changes(before: dict[str, Any], after: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    has_scope = any(rule["targets"] for rule in spec["requirements"])

    def add(old, new, group_key="", node_key="", path="", targets=None):
        actual_targets = targets or [{"group_key": group_key, "node_key": node_key, "field_path": path}]
        covered, requirement_ids = _scope_coverage(spec, actual_targets)
        status = "covered" if covered else "failed" if spec.get("scope_policy") == "strict" else "unverified"
        kind = "新增" if old is _MISSING else "删除" if new is _MISSING else "修改"
        changes.append({"kind": kind, "group_key": group_key, "node_key": node_key, "field_path": path,
                        "before": None if old is _MISSING else deepcopy(old), "after": None if new is _MISSING else deepcopy(new),
                        "before_present": old is not _MISSING, "after_present": new is not _MISSING,
                        "before_rule": None if old is _MISSING else deepcopy(old), "after_rule": None if new is _MISSING else deepcopy(new),
                        "impact": "已关联预期规则，结果仍需场景核对" if covered else "超出声明的修改范围，已阻断审批" if status == "failed" else "额外变更，需人工判断" if has_scope else "未声明修改范围，需人工判断",
                        "targets": actual_targets, "requirement_ids": requirement_ids,
                        "in_scope": covered if has_scope else None, "status": status})

    def walk(old, new, group_key="", node_key="", path=""):
        if old is not _MISSING and new is not _MISSING and canonical_json(old) == canonical_json(new):
            return
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(set(old) | set(new)):
                walk(old.get(key, _MISSING), new.get(key, _MISSING), group_key, node_key, path + "/" + key)
        else:
            add(old, new, group_key, node_key, path.lstrip("/"))

    def keyed(items, name):
        result = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str) or item["key"] in result:
                raise LogicReviewError(f"{name} 中出现缺失或重复的 key。")
            result[item["key"]] = item
        return result

    old_groups, new_groups = keyed(before["actionData"], "动作分组"), keyed(after["actionData"], "动作分组")
    old_order = [key for key in old_groups if key in new_groups]
    new_order = [key for key in new_groups if key in old_groups]
    if old_order != new_order:
        affected = [key for index, key in enumerate(new_order) if old_order[index] != key]
        add(old_order, new_order, path="actionData/$order", targets=[{"group_key": key, "node_key": "", "field_path": "$order"} for key in affected])
    for group_key in dict.fromkeys([*old_groups, *new_groups]):
        old, new = old_groups.get(group_key, _MISSING), new_groups.get(group_key, _MISSING)
        if old is _MISSING or new is _MISSING:
            add(old, new, group_key, path="$group")
            continue
        walk({k: v for k, v in old.items() if k != "data"}, {k: v for k, v in new.items() if k != "data"}, group_key)
        old_nodes, new_nodes = keyed(old.get("data", []), "节点"), keyed(new.get("data", []), "节点")
        old_common = [key for key in old_nodes if key in new_nodes]
        new_common = [key for key in new_nodes if key in old_nodes]
        if old_common != new_common:
            for index, node_key in enumerate(new_common):
                if old_common[index] != node_key:
                    add(old_common.index(node_key), index, group_key, node_key, "$order")
        for node_key in dict.fromkeys([*old_nodes, *new_nodes]):
            walk(old_nodes.get(node_key, _MISSING), new_nodes.get(node_key, _MISSING), group_key, node_key)
    for key in sorted((set(before) | set(after)) - {"actionData"}):
        walk(before.get(key, _MISSING), after.get(key, _MISSING), key, "", key)
    return changes


def _scenario_report(spec, baseline, candidate, report):
    scenario_spec = spec["scenarios"]
    if report is None:
        report = {"schema_version": 1, "spec_sha256": sha256_json(scenario_spec),
                  "baseline_source_sha256": sha256_text(str(baseline.get("js_code") or "")),
                  "candidate_source_sha256": sha256_text(str(candidate.get("js_code") or "")),
                  "runner_version": "not_run", "cases": [
                      {"id": case["id"], "title": case.get("title", case["id"]), "expected": deepcopy(case.get("expect", {})),
                       "requirement_ids": deepcopy(case.get("requirement_ids", [])),
                       "baseline": {"status": "unverified", "reason": "场景未执行", "actual": {}, "trace": []},
                       "candidate": {"status": "unverified", "reason": "场景未执行", "actual": {}, "trace": []}}
                      for case in scenario_spec["cases"]], "browser_verified": False, "runtime_verified": False}
    return deepcopy(report)


_TARGET_LIMIT_REASONS = {
    "未指定需求目标，需人工判断",
    "需求目标不存在或类型未知，需人工判断",
    "C# 需求目标未执行验证，JS 场景不能证明此项通过",
    "关联场景没有记录目标分组的执行，需人工判断",
    "仅有分组执行记录，需求目标节点未验证",
    "仅有分组执行记录，需求目标字段未验证",
}


def _requirement_target_limit(requirement, linked, candidate):
    targets = requirement["targets"]
    if not targets:
        return "未指定需求目标，需人工判断"
    groups = {group["key"]: group for group in candidate["actionData"]}
    for target in targets:
        group = groups.get(target["group_key"])
        if (group is None or group.get("actionType") not in {"js", "csharp"}
                or target.get("node_key") and not any(node.get("key") == target["node_key"] for node in group.get("data", []))):
            return "需求目标不存在或类型未知，需人工判断"
        if group["actionType"] == "csharp":
            return "C# 需求目标未执行验证，JS 场景不能证明此项通过"
    observed = {entry.get("api") for case in linked for entry in (case["candidate"].get("trace") or []) if isinstance(entry, dict)}
    executed = {key for key in groups if "group." + key in observed}
    # Event/registration traces do not identify the native callback function.
    # An unexecuted or later-overwritten canvas binding cannot prove which
    # group handled an event. Nested action/node coverage is also not logged.
    if any(target["group_key"] not in executed for target in targets):
        return "关联场景没有记录目标分组的执行，需人工判断"
    if any(target.get("node_key") for target in targets):
        return "仅有分组执行记录，需求目标节点未验证"
    if any(target.get("field_path") for target in targets):
        return "仅有分组执行记录，需求目标字段未验证"
    return None


def _requirements_evidence(spec, report, candidate=None, canvas_checks=None):
    evidence = []
    for requirement in spec["requirements"]:
        linked = [case for case in report["cases"] if requirement["id"] in case.get("requirement_ids", [])]
        statuses = {case["candidate"]["status"] for case in linked}
        status = "failed" if "failed" in statuses else "unverified" if not linked or "unverified" in statuses else "passed"
        reason = ("没有关联执行场景，需人工判断" if not linked else "关联场景存在失败" if status == "failed" else
                  "关联场景仍有未验证项" if status == "unverified" else "关联场景均通过；覆盖范围限于这些场景")
        if status == "passed" and candidate is not None:
            limit = _requirement_target_limit(requirement, linked, candidate)
            if limit:
                status, reason = "unverified", limit
        item = {**deepcopy(requirement), "status": status, "case_ids": [case["id"] for case in linked], "reason": reason}
        configuration = [check for check in (canvas_checks or []) if requirement["id"] in check["requirement_ids"]]
        if configuration:
            states = {check["status"] for check in configuration}
            # A passing local assertion covers only its actual value. Broad
            # group/node targets and array-member checks cannot certify an
            # entire requirement's configuration scope.
            covered = all(target.get("node_key") and target.get("field_path") and any(
                check["operator"] in {"equals", "unchanged"}
                and check["target"]["group_key"] == target["group_key"]
                and check["target"]["node_key"] == target["node_key"]
                and (target["field_path"].lstrip("/") == "paramsValue" + check["path"]
                     or target["field_path"].lstrip("/").startswith("paramsValue" + check["path"] + "/"))
                for check in configuration) for target in requirement["targets"])
            configuration_status = "failed" if "failed" in states else "unverified" if "unverified" in states or not covered else "passed"
            configuration_reason = ("关联配置断言存在失败" if configuration_status == "failed" else
                                    "关联配置断言仍有未验证项" if "unverified" in states else
                                    "指定配置字段已核对；运行状态独立" if covered else
                                    "关联断言已通过；配置范围尚未完整核对")
            item.update(configuration_status=configuration_status, configuration_reason=configuration_reason,
                        configuration_check_ids=[check["id"] for check in configuration])
        evidence.append(item)
    return evidence


def _has_expectations(expect):
    return bool(expect.get("fields") or expect.get("call_counts")) or any(
        field in expect for field in ("metadata", "metadata_registrations", "last_return", "last_return_undefined", "call_order"))


def _review_summary(spec, changes, report, requirements=None, canvas_checks=None):
    counts = {status: sum(case["candidate"]["status"] == status for case in report["cases"]) for status in _STATUSES}
    scope_unknown = sum(change["status"] == "unverified" for change in changes)
    scope_failed = sum(change["status"] == "failed" for change in changes)
    if requirements is None:
        requirements = _requirements_evidence(spec, report)
    requirement_counts = {"requirements_" + status: sum(item["status"] == status for item in requirements) for status in _STATUSES}
    failures = counts["failed"] + scope_failed
    unknowns = counts["unverified"] + scope_unknown + requirement_counts["requirements_unverified"] + int(not spec["requirements"])
    summary = {**counts, **requirement_counts, "scope_failed": scope_failed, "scope_unverified": scope_unknown}
    if canvas_checks is not None:
        configuration_counts = {"configuration_" + state: sum(check["status"] == state for check in canvas_checks) for state in _STATUSES}
        summary.update(configuration_counts)
        failures += configuration_counts["configuration_failed"]
        unknowns += configuration_counts["configuration_unverified"]
    gate = {"blocked": bool(failures), "fail_count": failures, "unknown_count": unknowns,
            "pass_count": counts["passed"] + (summary.get("configuration_passed", 0))}
    return summary, gate, "failed" if failures else "unverified" if unknowns else "passed"


def build_business_review(*, review_spec, baseline_design, candidate_design, compiler_binding, scenario_report=None):
    spec = normalize_review_spec(review_spec)
    hashes = _candidate_hashes(candidate_design)
    if "relation_sha256" not in hashes:
        raise LogicReviewError("候选业务审查缺少 relation_data。")
    candidate = _canvas(candidate_design)
    baseline = _canvas(baseline_design)
    changes = _canvas_changes(baseline, candidate, spec)
    scenarios = _scenario_report(spec, baseline_design, candidate_design, scenario_report)
    try:
        canvas_checks = build_canvas_checks(spec["canvas_assertions"], baseline, candidate) if "canvas_assertions" in spec else None
        requirements = _requirements_evidence(spec, scenarios, candidate, canvas_checks)
        summary, gate, status = _review_summary(spec, changes, scenarios, requirements, canvas_checks)
    except CanvasAssertionError as exc:
        raise LogicReviewError(str(exc)) from exc
    except (KeyError, TypeError) as exc:
        raise LogicReviewError("场景执行报告结构无效。") from exc
    review = {"schema_version": 1, "engine_version": REVIEW_ENGINE_VERSION, "spec": spec,
              "changes": changes, "scenarios": scenarios, "requirements": requirements,
              "bindings": {"baseline_content_hash": _base_hash(baseline_design),
                           "baseline_js_sha256": sha256_text(str(baseline_design.get("js_code") or "")),
                           "candidate_hashes": hashes, "compiler_binding_sha256": compiler_binding.get("compiler_binding_sha256"),
                           "review_spec_sha256": sha256_json(spec)},
              "summary": summary, "candidate_gate": gate, "status": status,
              "browser_verified": False, "runtime_verified": False}
    if canvas_checks is not None:
        review["canvas_checks"] = canvas_checks
    review["review_sha256"] = sha256_json(review)
    validate_business_review(review, baseline_design=baseline_design, candidate_design=candidate_design, compiler_binding=compiler_binding)
    return review


def validate_business_review(review, *, baseline_design=None, candidate_design=None, candidate_hashes=None, compiler_binding=None):
    if not isinstance(review, dict) or type(review.get("schema_version")) is not int or review.get("schema_version") != REVIEW_SCHEMA_VERSION or review.get("engine_version") != REVIEW_ENGINE_VERSION:
        raise LogicReviewError("业务审查版本缺失或不受支持，请重新生成计划。")
    if review.get("review_sha256") != sha256_json({k: v for k, v in review.items() if k != "review_sha256"}):
        raise LogicReviewError("业务审查报告摘要无效，请重新生成计划。")
    spec = normalize_review_spec(review.get("spec"), legacy_scope=True)
    if spec != review.get("spec"):
        raise LogicReviewError("业务审查预期规则未规范化。")
    binding = review.get("bindings")
    if not isinstance(binding, dict) or binding.get("review_spec_sha256") != sha256_json(spec):
        raise LogicReviewError("业务审查未绑定当前预期规则。")
    for field in ("baseline_content_hash", "baseline_js_sha256", "compiler_binding_sha256", "review_spec_sha256"):
        if not isinstance(binding.get(field), str) or not _HASH.fullmatch(binding[field]):
            raise LogicReviewError("业务审查绑定摘要无效：" + field)
    hashes = binding.get("candidate_hashes")
    if not isinstance(hashes, dict) or set(hashes) != {"data_sha256", "js_sha256", "csharp_sha256", "relation_sha256"} or any(
        not isinstance(value, str) or not _HASH.fullmatch(value) for value in hashes.values()
    ):
        raise LogicReviewError("业务审查候选摘要不完整。")
    if candidate_hashes is not None and hashes != candidate_hashes:
        raise LogicReviewError("业务审查不属于当前候选内容。")
    if candidate_design is not None and any(hashes.get(key) != value for key, value in _candidate_hashes(candidate_design).items()):
        raise LogicReviewError("业务审查与当前画布或生成代码不一致。")
    if baseline_design is not None and (binding["baseline_content_hash"] != _base_hash(baseline_design)
        or binding["baseline_js_sha256"] != sha256_text(str(baseline_design.get("js_code") or ""))):
        raise LogicReviewError("业务审查基线已变化。")
    if compiler_binding is not None and binding["compiler_binding_sha256"] != compiler_binding.get("compiler_binding_sha256"):
        raise LogicReviewError("业务审查编译器已变化。")
    changes, report = review.get("changes"), review.get("scenarios")
    if not isinstance(changes, list) or not isinstance(report, dict) or not isinstance(report.get("cases"), list):
        raise LogicReviewError("业务审查差异或场景报告缺失。")
    strict_scope = spec.get("scope_policy") == "strict"
    valid_change_statuses = {"covered", "failed"} if strict_scope else {"covered", "unverified"}
    if any(not isinstance(change, dict) or change.get("status") not in valid_change_statuses for change in changes):
        raise LogicReviewError("业务审查差异状态无效。")
    if strict_scope:
        for change in changes:
            targets = change.get("targets")
            if (not isinstance(targets, list) or not targets or any(not isinstance(target, dict)
                    or not isinstance(target.get("group_key"), str)
                    or any(key in target and not isinstance(target[key], str) for key in ("node_key", "field_path"))
                    for target in targets)):
                raise LogicReviewError("严格修改范围的差异目标证据无效。")
            covered, identifiers = _scope_coverage(spec, targets)
            if (change.get("in_scope") is not covered or change.get("status") != ("covered" if covered else "failed")
                    or change.get("requirement_ids") != identifiers):
                raise LogicReviewError("严格修改范围的差异状态与需求目标不一致。")
    if baseline_design is not None and candidate_design is not None and changes != _canvas_changes(_canvas(baseline_design), _canvas(candidate_design), spec):
        raise LogicReviewError("业务审查未包含真实完整画布差异。")
    if type(report.get("schema_version")) is not int or report.get("schema_version") != 1 or report.get("spec_sha256") != sha256_json(spec["scenarios"]):
        raise LogicReviewError("场景报告未绑定当前预期。")
    _text(report.get("runner_version"), "scenario_report.runner_version", limit=128)
    if report.get("baseline_source_sha256") != binding["baseline_js_sha256"] or report.get("candidate_source_sha256") != hashes["js_sha256"]:
        raise LogicReviewError("场景报告未绑定实际生成代码。")
    cases = spec["scenarios"]["cases"]
    if len(report["cases"]) != len(cases):
        raise LogicReviewError("场景报告覆盖范围不完整。")
    for expected, actual in zip(cases, report["cases"]):
        if not isinstance(actual, dict) or actual.get("id") != expected["id"] or actual.get("expected") != expected.get("expect", {}) or actual.get("requirement_ids", []) != expected.get("requirement_ids", []):
            raise LogicReviewError("场景报告与固定预期不一致。")
        for side in ("baseline", "candidate"):
            if not isinstance(actual.get(side), dict) or actual[side].get("status") not in _STATUSES:
                raise LogicReviewError("场景结果状态无效。")
            if actual[side]["status"] == "passed" and not _has_expectations(expected.get("expect", {})):
                raise LogicReviewError("没有显式预期断言的场景不能声明通过。")
            assertions = actual[side].get("assertions")
            if actual[side]["status"] == "passed" and assertions is not None and (
                not isinstance(assertions, list) or not assertions or any(not isinstance(item, dict) or item.get("passed") is not True for item in assertions)
            ):
                raise LogicReviewError("场景通过状态与实际断言结果不一致。")
    canvas_checks = None
    if "canvas_assertions" in spec:
        try:
            canvas_checks = build_canvas_checks(spec["canvas_assertions"],
                _canvas(baseline_design) if baseline_design is not None else None,
                _canvas(candidate_design) if candidate_design is not None else None,
                recorded=review.get("canvas_checks"))
        except CanvasAssertionError as exc:
            raise LogicReviewError(str(exc)) from exc
        if review.get("canvas_checks") != canvas_checks:
            raise LogicReviewError("画布配置检查缺失或不一致。")
    elif "canvas_checks" in review:
        raise LogicReviewError("没有固定画布断言时不能添加配置检查。")
    requirements = _requirements_evidence(spec, report, _canvas(candidate_design) if candidate_design is not None else None, canvas_checks)
    if candidate_design is None:
        # Read-only report validation may lack the canvas. Preserve a recorded
        # conservative limit; every write validates it again with the candidate.
        for expected, recorded in zip(requirements, review.get("requirements") or []):
            if (expected["status"] == "passed" and isinstance(recorded, dict) and recorded.get("status") == "unverified"
                    and recorded.get("reason") in _TARGET_LIMIT_REASONS):
                expected.update(status="unverified", reason=recorded["reason"])
    summary, gate, status = _review_summary(spec, changes, report, requirements, canvas_checks)
    if review.get("requirements") != requirements:
        raise LogicReviewError("业务审查逐条需求状态与关联场景不一致。")
    if review.get("summary") != summary or review.get("candidate_gate") != gate or review.get("status") != status:
        raise LogicReviewError("业务审查汇总与实际场景结果不一致。")
    if review.get("browser_verified") is not False or review.get("runtime_verified") is not False:
        raise LogicReviewError("本地业务审查不能声明浏览器或运行环境验收通过。")


def plan_binding_hash(artifact: dict[str, Any]) -> str:
    binding = {"schema_version": 1, "base_content_hash": artifact.get("base_content_hash"),
                        "compiler_binding": artifact.get("compiler_binding"), "hashes": artifact.get("hashes"),
                        "business_review": artifact.get("business_review")}
    if "action_context" in artifact:
        binding["action_context"] = artifact["action_context"]
    if "generation" in artifact:
        binding["generation"] = artifact["generation"]
    for field in ("generation_mode", "generation_selection"):
        if field in artifact:
            binding[field] = artifact[field]
    return sha256_json(binding)


def require_write_business_review(artifact: dict[str, Any]) -> None:
    if artifact.get("kind") != "write":
        return
    review = artifact.get("business_review")
    if not isinstance(review, dict):
        raise LogicReviewError("旧审批缺少业务审查，请取消旧审批并重新生成计划。")
    compiler = artifact.get("compiler_evidence")
    if not isinstance(compiler, dict) or not isinstance(compiler.get("hashes"), dict) or not isinstance(compiler.get("compiler_binding"), dict) or not isinstance(artifact.get("desired_design"), dict):
        raise LogicReviewError("写入业务审查缺少候选内容或编译器绑定。")
    validate_business_review(review, candidate_design=artifact.get("desired_design"),
                             candidate_hashes=compiler.get("hashes"), compiler_binding=compiler.get("compiler_binding"))
    if "scope_policy" not in review["spec"]:
        raise LogicReviewError("旧审批缺少修改范围策略，只可查看；请取消旧审批并重新生成计划。")
    if review["bindings"]["baseline_content_hash"] != artifact.get("old_content_hash"):
        raise LogicReviewError("业务审查不属于当前事务基线。")
    if review["candidate_gate"]["blocked"]:
        raise LogicReviewError("场景核对失败，不能进入审批或执行。")
    report = review["scenarios"]
    if report["cases"]:
        from .logic_scenarios import RUNNER_VERSION
        if report["runner_version"] == "not_run":
            if any(case["candidate"]["status"] != "unverified" for case in report["cases"]):
                raise LogicReviewError("未运行场景不能声明通过或失败。")
        elif report["runner_version"] != RUNNER_VERSION:
            raise LogicReviewError("场景验证器版本已变化，请重新生成计划。")
