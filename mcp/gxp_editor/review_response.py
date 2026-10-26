"""Small transport responses; complete bound evidence remains in plan artifacts."""
from __future__ import annotations

import json
from copy import deepcopy
from .value_transport import field_value, value_page, value_summary


def evidence_summary(evidence):
    """Bound transport metadata without altering the complete approval evidence."""
    result = {}
    for key, value in (evidence or {}).items():
        if isinstance(value, (list, dict)):
            result[key + "_count"] = len(value)
        elif isinstance(value, str):
            result[key] = value[:1000]
        elif value is None or type(value) in (bool, int, float):
            result[key] = value
    return result


def review_response(review, plan_id, detail="summary"):
    if detail not in {"summary", "full"}:
        raise ValueError("response_detail must be summary or full")
    issues = []
    for section, rows in (("canvas_checks", review.get("canvas_checks", [])),
                          ("changes", review.get("changes", [])),
                          ("requirements", review.get("requirements", [])),
                          ("scenarios", (review.get("scenarios") or {}).get("cases", []))):
        for index, row in enumerate(rows):
            evidence = row.get("candidate", {}) if section == "scenarios" else row
            if evidence.get("status") not in {"failed", "unverified"}:
                continue
            issues.append({"section": section, "index": index, "id": row.get("id"),
                           "status": evidence["status"], "reason": str(evidence.get("reason") or row.get("impact") or "")[:400],
                           "target": deepcopy(row.get("target") or {key: row[key] for key in
                               ("group_key", "node_key", "field_path") if key in row})})
    issues.sort(key=lambda item: item["status"] != "failed")
    result = {"review_status": review["status"], "review_summary": deepcopy(review.get("summary") or {}),
              "report_id": plan_id, "report_sha256": review["review_sha256"],
              "review_issues": issues[:5], "additional_issue_count": max(0, len(issues) - 5),
              "evidence_tool": "read_plan_review", "scope_policy": review["spec"].get("scope_policy", "legacy_manual_review")}
    if detail == "full":
        result["review_report"] = review
    return result


def _row_summary(row):
    result, fields = {}, []
    identity = {"id", "title", "status", "reason", "group_key", "node_key", "field_path", "impact", "source"}
    for key, value in row.items():
        if key in identity and (value is None or type(value) in (bool, int, float) or type(value) is str and len(value) <= 240):
            result[key] = deepcopy(value)
        else:
            pointer = "/" + key.replace("~", "~0").replace("/", "~1")
            fields.append({"field_path": pointer, **value_summary(value)})
    for key in ("candidate", "baseline"):
        if type(row.get(key)) is dict and "status" in row[key]:
            result[key + "_status"] = row[key]["status"]
    result.update(fields=fields[:20], field_count=len(fields), more_fields=len(fields) > 20)
    return result


def review_page(review, section="summary", offset=0, limit=10, item_id="", *, detail="full",
                field_path="", value_offset=0, max_output_bytes=16384):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Use offset >= 0 and limit between 1 and 20")
    if detail not in {"summary", "full"}:
        raise ValueError("detail must be summary or full")
    if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 65536:
        raise ValueError("max_output_bytes must be between 1024 and 65536")
    if section == "summary":
        if field_path:
            raise ValueError("Select a report section and row before reading a field")
        return {"section": section, "summary": deepcopy(review["summary"]),
                "candidate_gate": deepcopy(review["candidate_gate"]), "status": review["status"]}
    collections = {"requirements": review["requirements"], "changes": review["changes"],
                   "scenarios": review["scenarios"]["cases"], "canvas_checks": review.get("canvas_checks", []),
                   "spec": [review["spec"]]}
    if section not in collections:
        raise ValueError("section must be summary, requirements, changes, scenarios, canvas_checks or spec")
    rows = collections[section]
    if item_id:
        rows = [row for row in rows if row.get("id") == item_id]
    if field_path:
        if max_output_bytes < 2048:
            raise ValueError("Field continuation requires max_output_bytes >= 2048")
        if offset >= len(rows):
            raise ValueError("The selected report row does not exist")
        if item_id and len(rows) != 1:
            raise ValueError("item_id must identify exactly one report row")
        row = rows[offset]
        metadata = {"section": section, "item_id": row.get("id"), "row_offset": offset, "field_path": field_path}
        overhead = len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) + 128
        if max_output_bytes - overhead < 1024:
            raise ValueError("Byte budget is too small for this report field; request a larger max_output_bytes")
        page = value_page(field_value(row, field_path), offset=value_offset, max_output_bytes=max_output_bytes - overhead)
        return {**metadata, **page, "max_output_bytes": max_output_bytes}
    page, size = [], 0
    for row in rows[offset:offset + limit]:
        if detail == "summary":
            row = _row_summary(row)
        length = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
        budget = max_output_bytes - 512 if detail == "summary" else 256 * 1024
        if size + length > budget:
            if not page:
                return {"section": section, "items": [], "offset": offset, "total": len(rows),
                        "oversized_item": {"index": offset, "id": row.get("id"), "bytes": length},
                        "hint": "Use this section/row with field_path and value_offset for bounded exact evidence. Complete evidence remains in the plan and formal approval window; open_plan_review(response_detail='full') is only for an explicitly requested standalone preview."}
            break
        page.append(deepcopy(row))
        size += length
    next_offset = offset + len(page)
    return {"section": section, "items": page, "offset": offset, "total": len(rows), "detail": detail,
            "next_offset": next_offset if next_offset < len(rows) else None}
