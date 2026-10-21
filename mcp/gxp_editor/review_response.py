"""Small transport responses; complete bound evidence remains in plan artifacts."""
from __future__ import annotations

import json
from copy import deepcopy


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


def review_page(review, section="summary", offset=0, limit=10, item_id=""):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Use offset >= 0 and limit between 1 and 20")
    if section == "summary":
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
    page, size = [], 0
    for row in rows[offset:offset + limit]:
        length = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
        if size + length > 256 * 1024:
            if not page:
                return {"section": section, "items": [], "offset": offset, "total": len(rows),
                        "oversized_item": {"index": offset, "id": row.get("id"), "bytes": length},
                        "hint": "Complete evidence remains in the plan and formal approval window. Only if the user explicitly requests a standalone preview, use open_plan_review(response_detail='full'). Do not launch it automatically."}
            break
        page.append(deepcopy(row))
        size += length
    next_offset = offset + len(page)
    return {"section": section, "items": page, "offset": offset, "total": len(rows),
            "next_offset": next_offset if next_offset < len(rows) else None}
