"""Snapshot identity displayed alongside an edit; never infer ownership by name."""
from __future__ import annotations


def action_context(baseline):
    baseline = baseline or {}
    metadata = baseline.get("metadata") or {}
    kind = metadata.get("action_type")
    if kind not in {"form", "public"}:
        kind = "form" if metadata.get("page_id") else "unknown"
    context = {
        "schema_version": 1,
        "action_type": kind,
        "action_name": str(metadata.get("action_name") or baseline.get("name") or ""),
        "action_code": str(metadata.get("action_code") or baseline.get("code") or ""),
        "ref_id": str(baseline.get("ref_id") or ""),
        "design_id": str(baseline.get("id") or baseline.get("design_id") or ""),
        "page_id": str(metadata.get("page_id") or "") if kind != "public" else "",
        "form_name": str(metadata.get("form_name") or "") if kind != "public" else "",
        "page_record_id": str(metadata.get("page_record_id") or "") if kind != "public" else "",
        "page_route": str(metadata.get("page_route") or "") if kind != "public" else "",
        "application_id": str(metadata.get("application_id") or baseline.get("cpm_app_id") or ""),
        "page_resolution": str(metadata.get("page_resolution") or "unknown") if kind != "public" else "not_applicable",
    }
    return context


def context_summary(context):
    if context.get("action_type") == "public":
        owner = "公共动作（可被多个表单调用）"
    else:
        owner = context.get("form_name") or "所属表单待确认"
    action = context.get("action_name") or "动作名称待确认"
    return owner + "  →  " + action


def context_details(context):
    rows = [context_summary(context)]
    for title, key in (("页面标识", "page_id"), ("页面路由", "page_route"), ("页面记录 ID", "page_record_id"),
                       ("动作编号", "action_code"), ("动作 ID", "ref_id"), ("草稿 ID", "design_id"), ("应用 ID", "application_id")):
        if context.get(key):
            rows.append(title + "：" + str(context[key]))
    return "\n".join(rows)
