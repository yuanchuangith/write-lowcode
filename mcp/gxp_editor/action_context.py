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


def publication_target(context, session=None):
    """Keep the saved action's identity available even without its artifact."""
    target = dict(context or {})
    session = session or {}
    if any(target.get(key) and session.get(key) and target[key] != session[key]
           for key in ("ref_id", "design_id")):
        target = {}
    for key in ("action_name", "action_code", "ref_id", "design_id"):
        target[key] = str(session.get(key) or target.get(key) or "")
    return target


def queue_publication_targets(items, conversation_id):
    """List completed saves from this queue, including after reopening it."""
    targets, seen = [], set()
    for item in items:
        if (item["conversation_id"] != conversation_id
                or item["owner_revision"] != item["queued_owner_revision"]
                or item.get("transaction_state") != "saved" or item["state"] != "finished"):
            continue
        target = publication_target(item.get("action_context"), item)
        identity = target["ref_id"] or target["design_id"]
        if identity not in seen:
            seen.add(identity)
            targets.append(target)
    return targets


def publication_notice(targets):
    if not targets:
        return ""
    lines = ["请在平台手动重新发布以下动作："]
    for index, target in enumerate(targets, 1):
        name = target.get("action_name") or "动作名称待确认"
        code = target.get("action_code") or target.get("ref_id") or target.get("design_id") or "编号待确认"
        owner = "公共动作" if target.get("action_type") == "public" else target.get("form_name") or "所属表单待确认"
        lines.append(f"{index}. {name}（{code}） · {owner}")
    return "\n".join(lines)
