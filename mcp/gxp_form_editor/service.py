"""Form workflow. Only the local human approval application calls approve/execute."""
from __future__ import annotations

import copy
import hmac
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .common import DOMAIN, FormError, canonical, digest, file_lock, pointer, timestamp
from .state import Store


class FormService:
    def __init__(self, conversation_id: str = "", *, store: Store | None = None, client=None,
                 secret: bytes | None = None, launcher=None):
        from .platform import PlatformClient
        self.owner = conversation_id
        self.store = store or Store()
        self.client = client or PlatformClient()
        self._secret = secret
        self.launcher = launcher or self._launch

    @property
    def secret(self):
        if self._secret is None:
            from .approval import signing_secret
            self._secret = signing_secret()
        return self._secret

    def _owned(self):
        self.store.require_conversation(self.owner)

    def create_form_conversation(self, label: str = "") -> dict:
        record = self.store.create_conversation(label)
        return {"conversation_id": record["id"], "domain": DOMAIN}

    def form_editor_status(self) -> dict:
        return {"domain": DOMAIN, **self.client.readiness(), "automatic_publication": False,
                "supported_operations": ["set_property", "set_default", "set_layout", "set_binding", "set_filter"]}

    def form_connection_settings(self, operation="status") -> dict:
        from .platform import load_settings
        config = load_settings()
        if operation == "open":
            if config.get("project_binding"):
                from gxp_editor.connection_settings import manage_settings
                return manage_settings("open")
            return self.launcher("connection", "")
        if operation != "status":
            raise FormError("invalid_operation", "设置入口仅支持 status/open；密码只在本机窗口输入。")
        return {"configured": bool(config), "base_url": config.get("base_url", ""),
                "environment": config.get("environment", ""), **self.client.readiness()}

    def begin_form_edit_session(self, target: dict) -> dict:
        from .schema import find_components
        self._owned()
        snapshot = self.client.read_draft(target)
        if snapshot.get("identity") != target or not isinstance(snapshot.get("page"), dict):
            raise FormError("draft_identity_mismatch", "草稿响应与指定页面及版本不一致。")
        catalog = self.client.catalog()
        backup = self.store.save_backup(snapshot, self.secret)
        session = self.store.create_session(self.owner, target, snapshot, backup)
        session["catalog_hash"] = digest(catalog)
        session = self.store.replace("session", session, self.owner, session["revision"])
        return {"session_id": session["id"], "target": target, "state": session["state"],
                "backup": backup, "baseline_hash": digest(snapshot), "catalog_available": bool(catalog.get("components")),
                "component_count": len(find_components(snapshot["page"])), **self.client.readiness()}

    def _session(self, session_id: str, *, active=False) -> dict:
        self._owned()
        session = self.store.get("session", session_id, self.owner)
        if active and session["state"] not in {"active", "planned"}:
            raise FormError("session_not_editable", "当前状态不能继续规划，请先核对保存或审批状态。")
        original = self.store.load_backup(session["backup"], self.secret)
        if digest(original) != digest(session["snapshot"]) or original.get("identity") != session["target"]:
            raise FormError("baseline_changed", "会话基线与已签名的不可变备份不一致。")
        return session

    def find_form_components(self, session_id: str, query="", offset=0, limit=30) -> dict:
        from .schema import find_components
        session = self._session(session_id)
        rows = find_components(session["snapshot"]["page"])
        query = query.casefold()
        rows = [row for row in rows if not query or query in canonical(row).casefold()]
        return self._page(rows, offset, limit)

    def inspect_form_component(self, session_id: str, component_id: str, response_detail="summary") -> dict:
        from .schema import inspect_component
        session = self._session(session_id)
        component = inspect_component(session["snapshot"]["page"], component_id)
        catalog = self.client.catalog()
        if digest(catalog) != session["catalog_hash"]:
            raise FormError("catalog_changed", "组件属性合约已变化，请重新建立表单会话。")
        contract = catalog.get("components", {}).get(component["component_type"], {})
        if response_detail not in {"summary", "full"}:
            raise FormError("invalid_detail", "详情格式必须是 summary 或 full。")
        result = {key: value for key, value in component.items() if key != "schema"}
        result.update({"editable": bool(contract), "contract": contract, "baseline_hash": digest(session["snapshot"])})
        if response_detail == "full" and len(canonical(component["schema"])) <= 32768:
            result["schema"] = component["schema"]
            return result
        properties = {}
        budget = 16384
        for path in contract.get("properties", {}):
            try:
                value = pointer(component["schema"], path)
            except FormError:
                properties[path] = {"exists": False}
                continue
            text = canonical(value)
            if len(text) <= min(2048, budget):
                properties[path] = {"exists": True, "value": value}
                budget -= len(text)
            else:
                properties[path] = {"exists": True, "value_hash": digest(value), "characters": len(text),
                                    "read_tool": "read_form_value"}
        result.update({"properties": properties, "schema_hash": digest(component["schema"]), "response_detail": "summary"})
        return result

    def inspect_form_resource(self, session_id: str, kind: str, resource_id: str) -> dict:
        session = self._session(session_id)
        if kind not in {"datasets", "dictionaries"} or not resource_id or len(resource_id) > 128:
            raise FormError("invalid_resource", "只能查询明确的数据集或字典标识。")
        resolver = getattr(self.client, "read_resource", None)
        if resolver is None:
            raise FormError("resource_unavailable", "当前平台没有已验证的资源读取适配器。")
        return resolver(session["snapshot"], kind, resource_id)

    def read_form_value(self, session_id: str, component_id: str, path: str, offset=0, max_chars=8192) -> dict:
        from .schema import inspect_component
        session = self._session(session_id)
        component = inspect_component(session["snapshot"]["page"], component_id)
        value = pointer(component["schema"], path)
        encoded = canonical(value)
        if offset < 0 or not 256 <= max_chars <= 16384 or offset > len(encoded):
            raise FormError("invalid_paging", "读取偏移或预算无效。")
        end = min(len(encoded), offset + max_chars)
        return {"text": encoded[offset:end], "value_hash": digest(value), "offset": offset,
                "next_offset": end if end < len(encoded) else None, "complete": offset == 0 and end == len(encoded),
                "continuation_complete": end == len(encoded),
                **({"value": value} if offset == 0 and end == len(encoded) else {})}

    @staticmethod
    def _page(rows: list, offset: int, limit: int) -> dict:
        if offset < 0 or not 1 <= limit <= 100:
            raise FormError("invalid_paging", "分页范围无效。")
        end = min(len(rows), offset + limit)
        return {"items": rows[offset:end], "total": len(rows), "next_offset": end if end < len(rows) else None}

    def _fresh(self, session: dict) -> dict:
        fresh = self.client.read_draft(session["target"])
        if digest(fresh) != digest(session["snapshot"]):
            raise FormError("draft_changed", "页面内容、版本、账号或关联配置已变化，请重新建立会话。")
        return fresh

    def plan_form_patch(self, session_id: str, operations: list[dict], source_text: str) -> dict:
        from .schema import apply_operations
        session = self._session(session_id, active=True)
        if not source_text.strip():
            raise FormError("requirements_required", "请保留原始表单修改要求。")
        if len(canonical(operations).encode("utf-8")) > 2 * 1024 * 1024:
            raise FormError("operations_too_large", "修改清单超过 2 MiB。")
        self._fresh(session)
        catalog = self.client.catalog()
        if digest(catalog) != session["catalog_hash"]:
            raise FormError("catalog_changed", "组件属性合约已变化，请重新建立会话。")
        resources = self.client.resources(session["snapshot"], operations)
        result = apply_operations(session["snapshot"]["page"], operations, catalog, resources)
        if digest(result["page"]) == digest(session["snapshot"]["page"]):
            return {"no_changes": True, "plan_id": None, "session_released": False}
        readiness = self.client.readiness()
        requests = self.client.build_requests(session["snapshot"], result["page"]) if readiness.get("write_ready") else []
        plan = {"session_id": session_id, "source_text": source_text, "operations": operations,
                "candidate": result["page"], "changes": result["changes"], "warnings": result.get("warnings", []),
                "validation": result["validation"], "requests": requests, "catalog_hash": digest(catalog),
                "adapter_hash": session["snapshot"].get("source_binding", {}).get("adapter_hash", ""),
                "baseline_hash": digest(session["snapshot"]), "candidate_hash": digest(result["page"]),
                "resources_hash": digest(resources)}
        plan["plan_hash"] = digest(plan)
        with self.store.transaction() as connection:
            current = self.store.get("session", session_id, self.owner, connection)
            if current["revision"] != session["revision"] or current["state"] not in {"active", "planned"}:
                raise FormError("state_changed", "会话在规划时已变化。")
            plan = self.store.insert("plan", plan, self.owner, connection)
            self.store.replace("session", {**current, "plan_id": plan["id"], "state": "planned"}, self.owner, current["revision"], connection)
        return {"plan_id": plan["id"], "change_count": len(plan["changes"]), "changes": plan["changes"][:20],
                "warnings": plan["warnings"], "validation": plan["validation"], "write_ready": bool(requests),
                "preview_only": not bool(requests), "readiness": readiness}

    def read_form_plan(self, plan_id: str, section="changes", offset=0, limit=20, value_path="", value_offset=0, max_chars=8192) -> dict:
        self._owned()
        plan = self.store.get("plan", plan_id, self.owner)
        if section not in {"changes", "requests", "warnings", "operations"}:
            raise FormError("invalid_section", "仅支持 changes/requests/warnings/operations。")
        if value_path:
            if offset < 0 or offset >= len(plan[section]):
                raise FormError("invalid_paging", "记录偏移超出范围。")
            value = pointer(plan[section][offset], value_path)
            text = canonical(value)
            if value_offset < 0 or value_offset > len(text) or not 256 <= max_chars <= 16384:
                raise FormError("invalid_paging", "字段读取偏移或预算无效。")
            end = min(len(text), value_offset + max_chars)
            return {"plan_id": plan_id, "value_hash": digest(value), "text": text[value_offset:end],
                    "next_value_offset": end if end < len(text) else None, "continuation_complete": end == len(text)}
        result = self._page(plan[section], offset, limit)
        bounded = []
        remaining = 32768
        for item in result["items"]:
            text = canonical(item)
            if len(text) <= min(4096, remaining):
                bounded.append(item)
                remaining -= len(text)
            else:
                summary = {key: value for key, value in item.items() if key in
                           {"method", "path", "component_id", "component_type", "operation", "derived", "old_exists"}} if isinstance(item, dict) else {"text_preview": text[:512]}
                bounded.append({"summary": summary,
                                "value_hash": digest(item), "characters": len(text),
                                "read_hint": "使用同一记录 offset 和 value_path 分页读取字段。"})
        return {"plan_id": plan_id, **result, "items": bounded}

    def _plan_integrity(self, session: dict) -> dict:
        plan = self.store.get("plan", session.get("plan_id", ""), self.owner)
        projection = {key: value for key, value in plan.items()
                      if key not in {"id", "domain", "owner", "revision", "created_at", "plan_hash"}}
        if digest(projection) != plan["plan_hash"] or digest(plan["candidate"]) != plan["candidate_hash"]:
            raise FormError("plan_tampered", "计划内容指纹不一致。")
        if plan["baseline_hash"] != digest(session["snapshot"]) or plan["session_id"] != session["id"]:
            raise FormError("plan_tampered", "计划没有绑定当前备份基线。")
        return plan

    def _checked_plan(self, session: dict) -> dict:
        plan = self._plan_integrity(session)
        if digest(self.client.catalog()) != plan["catalog_hash"]:
            raise FormError("catalog_changed", "原生组件属性合约已变化。")
        if not self.client.readiness().get("write_ready"):
            raise FormError("platform_not_ready", "草稿隔离、并发或关联保存协议尚未验证，不能开启保存审批。", self.client.readiness())
        resource_snapshot = {**session["snapshot"], "page": plan["candidate"]} if plan.get("restores_session_id") else session["snapshot"]
        if digest(self.client.resources(resource_snapshot, plan.get("resource_operations", plan["operations"]))) != plan["resources_hash"]:
            raise FormError("resources_changed", "引用的数据集、字典或字段版本已变化，请重新规划。")
        if self.client.build_requests(session["snapshot"], plan["candidate"]) != plan["requests"]:
            raise FormError("request_changed", "平台请求或适配器已变化，请重新规划。")
        return plan

    def _binding(self, session, plan):
        return {"conversation_id": self.owner, "session_id": session["id"], "target": session["target"],
                "baseline_hash": plan["baseline_hash"], "candidate_hash": plan["candidate_hash"],
                "adapter_hash": plan["adapter_hash"], "requests": plan["requests"],
                "actor": session["snapshot"].get("actor"), "plan_hash": plan["plan_hash"]}

    def prepare_form_write(self, plan_id: str) -> dict:
        from .approval import prepare_approval
        self._owned()
        plan = self.store.get("plan", plan_id, self.owner)
        session = self._session(plan["session_id"])
        if session.get("plan_id") != plan_id or session["state"] not in {"planned", "reviewing"}:
            raise FormError("plan_not_current", "只能审批当前会话的最新计划。")
        self._checked_plan(session)
        self._fresh(session)
        expired_write = None
        if session.get("write_id"):
            write = self.store.get("write", session["write_id"], self.owner)
            if write["state"] in {"pending", "approved"}:
                if write["artifact"].get("expires_at", 0) > timestamp():
                    return {"write_id": write["id"], **self.launcher("approval", write["id"])}
                expired_write = write
        binding = self._binding(session, plan)
        artifact = prepare_approval(binding)
        with self.store.transaction() as connection:
            current = self.store.get("session", session["id"], self.owner, connection)
            if current["revision"] != session["revision"]:
                raise FormError("state_changed", "审批准备期间会话已变化。")
            if expired_write:
                self.store.replace("write", {**expired_write, "state": "expired"}, self.owner, expired_write["revision"], connection)
            write = self.store.insert("write", {"session_id": session["id"], "plan_id": plan_id,
                                       "artifact": artifact, "state": "pending", "receipts": [],
                                       "verified": False, "session_released": False}, self.owner, connection)
            self.store.replace("session", {**current, "state": "reviewing", "write_id": write["id"]}, self.owner, current["revision"], connection)
        return {"write_id": write["id"], **self.launcher("approval", write["id"])}

    def get_form_review(self, write_id: str, conversation_id: str | None = None) -> dict:
        if conversation_id is not None and conversation_id != self.owner:
            raise FormError("not_owned", "审批窗口不属于当前对话。")
        self._owned()
        write = self.store.get("write", write_id, self.owner)
        session = self._session(write["session_id"])
        plan = self._plan_integrity(session)
        approval_valid = False
        if write["state"] == "approved" and session.get("write_id") == write_id:
            from .approval import verify
            try:
                approval_valid = verify(write["artifact"], self.secret, expected_binding=self._binding(session, plan))
            except FormError:
                pass
        return {"write": write, "session": session, "plan": plan, "target": session["target"],
                "changes": plan["changes"], "requests": plan["requests"], "artifact": write["artifact"],
                "approval_valid": approval_valid}

    def approve_form_write(self, write_id: str, conversation_id: str | None = None, reviewer="local-user") -> dict:
        from .approval import approve
        review = self.get_form_review(write_id, conversation_id)
        session, write = review["session"], review["write"]
        if write["state"] != "pending" or session.get("write_id") != write_id:
            raise FormError("not_pending", "审批已处理或不再属于当前候选。")
        self._checked_plan(session)
        self._fresh(session)
        signed = approve(copy.deepcopy(write["artifact"]), self.secret, reviewer)
        with self.store.transaction() as connection:
            current = self.store.get("session", session["id"], self.owner, connection)
            if current["revision"] != session["revision"] or current["state"] != "reviewing":
                raise FormError("state_changed", "审批期间会话已变化。")
            write = self.store.replace("write", {**write, "state": "approved", "artifact": signed}, self.owner, write["revision"], connection)
        return {"write_id": write_id, "state": write["state"]}

    def execute_form_write(self, write_id: str, conversation_id: str | None = None) -> dict:
        self._owned()
        write = self.store.get("write", write_id, self.owner)
        with file_lock(self.store.root / "locks" / ("execute-" + write["session_id"] + ".lock")):
            return self._execute_form_write(write_id, conversation_id)

    def _execute_form_write(self, write_id: str, conversation_id: str | None = None) -> dict:
        from .approval import verify
        review = self.get_form_review(write_id, conversation_id)
        session, write = review["session"], review["write"]
        if write["state"] != "approved" or session.get("write_id") != write_id:
            raise FormError("not_approved", "保存必须使用当前人工批准的表单计划。")
        plan = self._checked_plan(session)
        verify(write["artifact"], self.secret, expected_binding=self._binding(session, plan))
        binding = write["artifact"]["binding"]
        if (binding.get("plan_hash") != plan["plan_hash"] or binding.get("conversation_id") != self.owner
                or binding.get("requests") != plan["requests"] or binding.get("session_id") != session["id"]):
            raise FormError("approval_mismatch", "审批与当前完整计划不一致。")
        self._fresh(session)
        verify(write["artifact"], self.secret, expected_binding=self._binding(session, plan))
        with self.store.transaction() as connection:
            current = self.store.get("session", session["id"], self.owner, connection)
            pending = self.store.get("write", write_id, self.owner, connection)
            if current["revision"] != session["revision"] or current["state"] != "reviewing" or pending["state"] != "approved":
                raise FormError("state_changed", "保存与取消或其它执行发生竞争。")
            write = self.store.replace("write", {**pending, "state": "executing", "started_at": timestamp()}, self.owner, pending["revision"], connection)
            self.store.replace("session", {**current, "state": "executing"}, self.owner, current["revision"], connection)
        try:
            for index, request in enumerate(plan["requests"]):
                receipt = self.client.execute_request(request)
                with self.store.transaction() as connection:
                    write = self.store.get("write", write_id, self.owner, connection)
                    write = self.store.replace("write", {**write, "receipts": [*write["receipts"], {"index": index, "result": receipt}]}, self.owner, write["revision"], connection)
            fresh = self.client.read_draft(session["target"])
            if not self._matches_saved(session["snapshot"], plan, fresh):
                raise FormError("saved_state_pending", "页面或关联数据未通过完整回读，请恢复核对。")
            return self._finish_saved(write_id, fresh, write["revision"])
        except Exception as error:
            code = error.code if isinstance(error, FormError) else "execution_uncertain"
            self._uncertain(write_id, code)
            return {"write_id": write_id, "state": "uncertain", "verified": False, "session_released": False,
                    "error_code": code, "next_action": "recover_form_edit_session"}

    def _matches_saved(self, baseline, plan, fresh):
        if fresh.get("identity") != baseline.get("identity") or fresh.get("actor") != baseline.get("actor"):
            return False
        if digest(fresh.get("page")) != plan["candidate_hash"]:
            return False
        checker = getattr(self.client, "verify_saved", None)
        if checker:
            return bool(checker(baseline, plan["candidate"], fresh, plan["requests"]))
        # An HTTP 2xx cannot prove associated permissions/models were saved.
        return not baseline.get("associated") and not fresh.get("associated") and len(plan["requests"]) == 1

    def _uncertain(self, write_id, reason):
        with self.store.transaction() as connection:
            write = self.store.get("write", write_id, self.owner, connection)
            session = self.store.get("session", write["session_id"], self.owner, connection)
            if write["state"] == "saved":
                return
            if write["state"] not in {"executing", "uncertain"} or session.get("write_id") != write_id:
                raise FormError("state_changed", "当前保存事务已变化。")
            self.store.replace("write", {**write, "state": "uncertain", "error_code": reason}, self.owner, write["revision"], connection)
            self.store.replace("session", {**session, "state": "uncertain"}, self.owner, session["revision"], connection)

    def _finish_saved(self, write_id, fresh, expected_revision):
        with self.store.transaction() as connection:
            write = self.store.get("write", write_id, self.owner, connection)
            session = self.store.get("session", write["session_id"], self.owner, connection)
            if (write["revision"] != expected_revision or write["state"] not in {"executing", "uncertain"}
                    or session.get("write_id") != write_id or session["state"] not in {"executing", "uncertain"}):
                raise FormError("state_changed", "保存核对期间流程已变化。")
            write = self.store.replace("write", {**write, "state": "saved", "verified": True,
                                       "session_released": True, "saved_snapshot": fresh,
                                       "saved_receipt_signature": self._receipt_signature(write_id, write["plan_id"], fresh)}, self.owner, write["revision"], connection)
            session = self.store.replace("session", {**session, "state": "saved", "verified": True,
                                           "session_released": True}, self.owner, session["revision"], connection)
            self.store.release(session, connection)
        return {"write_id": write_id, "state": "saved", "verified": True, "session_released": True,
                "published": False, "next_action": "在原生设计器检查草稿；发布需人工另行操作。"}

    def _receipt_signature(self, write_id, plan_id, snapshot):
        value = {"domain": DOMAIN, "write_id": write_id, "plan_id": plan_id, "snapshot_hash": digest(snapshot)}
        return hmac.new(self.secret, canonical(value).encode("utf-8"), "sha256").hexdigest()

    def get_form_write_status(self, write_id: str) -> dict:
        self._owned()
        write = self.store.get("write", write_id, self.owner)
        return {**{key: write.get(key) for key in ("id", "session_id", "plan_id", "state", "verified", "session_released", "error_code")},
                "receipts": [{"index": item["index"], "status": item["result"].get("status")} for item in write["receipts"]]}

    def cancel_form_edit_session(self, session_id: str) -> dict:
        self._owned()
        with self.store.transaction() as connection:
            session = self.store.get("session", session_id, self.owner, connection)
            if session["state"] not in {"active", "planned", "reviewing"}:
                raise FormError("cannot_cancel", "执行中、不确定或已保存的表单不能取消。")
            if session.get("write_id"):
                write = self.store.get("write", session["write_id"], self.owner, connection)
                if write["state"] not in {"pending", "approved"}:
                    raise FormError("cannot_cancel", "保存状态不能证明尚未执行。")
                self.store.replace("write", {**write, "state": "cancelled", "session_released": True}, self.owner, write["revision"], connection)
            session = self.store.replace("session", {**session, "state": "cancelled", "session_released": True}, self.owner, session["revision"], connection)
            self.store.release(session, connection)
        return {"session_id": session_id, "state": "cancelled", "session_released": True, "executed": False}

    def recover_form_edit_session(self, session_id: str) -> dict:
        self._owned()
        session = self.store.get("session", session_id, self.owner)
        with file_lock(self.store.root / "locks" / ("execute-" + session["id"] + ".lock")):
            return self._recover_form_edit_session(session_id)

    def _recover_form_edit_session(self, session_id: str) -> dict:
        from .approval import verify
        session = self._session(session_id)
        if session["state"] == "executing":
            # Holding the lifetime executor lock proves no live executor owns it.
            self._uncertain(session["write_id"], "executor_disappeared")
            session = self._session(session_id)
        if session["state"] != "uncertain":
            return {"session_id": session_id, "state": session["state"], "verified": session["verified"],
                    "session_released": session["session_released"], "replayed": False}
        plan = self._plan_integrity(session)
        write = self.store.get("write", session["write_id"], self.owner)
        artifact = write["artifact"]
        approved_at = (artifact.get("approval") or {}).get("approved_at")
        if not isinstance(approved_at, (int, float)):
            raise FormError("form_approval_required", "恢复核对需要原事务的有效人工审批签名。")
        # Historical signatures remain evidence after expiry; they never authorize replay.
        verify(artifact, self.secret, expected_binding=self._binding(session, plan), now=approved_at)
        if (artifact["binding"].get("plan_hash") != plan["plan_hash"]
                or artifact["binding"].get("requests") != plan["requests"]
                or artifact["binding"].get("session_id") != session_id
                or artifact["binding"].get("conversation_id") != self.owner):
            raise FormError("approval_mismatch", "原审批与待核对表单计划不一致。")
        fresh = self.client.read_draft(session["target"])
        if self._matches_saved(session["snapshot"], plan, fresh):
            return {**self._finish_saved(session["write_id"], fresh, write["revision"]), "replayed": False}
        return {"session_id": session_id, "state": "uncertain", "verified": False, "session_released": False,
                "replayed": False, "page_matches_baseline": digest(fresh.get("page")) == digest(session["snapshot"]["page"]),
                "next_action": "核对页面及关联保存结果；补偿修改须作为新的人工审批计划。"}

    def plan_form_restore(self, saved_session_id: str) -> dict:
        from .approval import verify
        from .schema import apply_operations, inspect_component, restore_approved_changes
        saved = self._session(saved_session_id)
        if saved["state"] != "saved" or not saved["verified"]:
            raise FormError("restore_unverified", "只有已核实保存的表单可建立备份恢复计划。")
        write = self.store.get("write", saved["write_id"], self.owner)
        plan = self._plan_integrity(saved)
        signed = write["artifact"]
        verify(signed, self.secret, expected_binding=self._binding(saved, plan), now=signed["approval"]["approved_at"])
        if not hmac.compare_digest(write.get("saved_receipt_signature", ""), self._receipt_signature(write["id"], plan["id"], write["saved_snapshot"])):
            raise FormError("saved_receipt_invalid", "保存回执签名无效，不能恢复。")
        baseline = self.store.load_backup(saved["backup"], self.secret)
        fresh = self.client.read_draft(saved["target"])
        if digest(fresh) != digest(write["saved_snapshot"]):
            raise FormError("restore_conflict", "保存后页面或关联内容已变化；不能覆盖后续修改。")
        if plan.get("restores_session_id"):
            raise FormError("restore_history_required", "恢复计划不自动再次取反；请针对当前配置提出新的修改计划。")
        catalog = self.client.catalog()
        if digest(catalog) != plan["catalog_hash"]:
            raise FormError("catalog_changed", "组件合约已变化，不能套用旧的恢复记录。")
        result = restore_approved_changes(fresh["page"], plan["changes"], catalog)
        if digest(result["page"]) != digest(baseline["page"]):
            raise FormError("restore_incomplete", "逆向属性变化不能完整还原已签名备份。")
        resource_operations = []
        for operation in plan["operations"]:
            if operation["operation"] not in {"set_binding", "set_default"}:
                continue
            component = inspect_component(result["page"], operation["component_id"])
            try:
                prior = pointer(component["schema"], operation["path"])
            except FormError:
                continue
            inverse = {**operation, "expected_old_value": prior, "value": prior}
            if operation["operation"] == "set_binding":
                descriptor = catalog["components"][operation["expected_component_type"]]["properties"][operation["path"]]
                adapter = catalog["components"][operation["expected_component_type"]].get("adapters", {}).get(descriptor.get("adapter") or "set_binding", {})
                kind = "datasets" if adapter.get("contract") in {"native-dataset-v1", "dynamic-list-dataset-v1"} else "dictionaries"
                old_id = prior.get("value") if isinstance(prior, dict) else prior
                if not old_id:
                    continue
                resource = self.client.read_resource(fresh, kind, old_id)
                if kind == "datasets" and prior.get("lastModificationTime") != resource.get("lastModificationTime"):
                    raise FormError("resources_changed", "备份引用的数据集版本已变化，不能直接恢复旧绑定。")
                inverse["value"] = {"id": old_id, "version": resource["version"]}
            resource_operations.append(inverse)
        restored_snapshot = {**fresh, "page": result["page"]}
        resources = self.client.resources(restored_snapshot, resource_operations)
        if resource_operations:
            # Validate restored selections/defaults against the live referenced
            # resources. The authenticated inverse diff remains the candidate;
            # a setter's re-evaluation is used only to check its contract.
            apply_operations(result["page"], resource_operations, catalog, resources)
        requests = self.client.build_requests(fresh, result["page"])
        started = self.begin_form_edit_session(saved["target"])
        try:
            session = self._session(started["session_id"], active=True)
            if digest(session["snapshot"]) != digest(fresh):
                raise FormError("restore_conflict", "建立恢复会话时草稿又发生变化。")
            restore_plan = {"session_id": session["id"], "source_text": "按已签名备份逆向恢复已核实的表单保存。",
                            "restores_session_id": saved_session_id, "operations": [], "resource_operations": resource_operations,
                            "candidate": result["page"], "changes": result["changes"], "warnings": result["warnings"],
                            "validation": result["validation"], "requests": requests, "catalog_hash": digest(catalog),
                            "adapter_hash": fresh.get("source_binding", {}).get("adapter_hash", ""),
                            "baseline_hash": digest(fresh), "candidate_hash": digest(result["page"]), "resources_hash": digest(resources)}
            restore_plan["plan_hash"] = digest(restore_plan)
            with self.store.transaction() as connection:
                current = self.store.get("session", session["id"], self.owner, connection)
                if current["revision"] != session["revision"] or current["state"] != "active":
                    raise FormError("state_changed", "恢复规划时会话已变化。")
                restore_plan = self.store.insert("plan", restore_plan, self.owner, connection)
                self.store.replace("session", {**current, "plan_id": restore_plan["id"], "state": "planned"}, self.owner, current["revision"], connection)
            return {"plan_id": restore_plan["id"], "change_count": len(result["changes"]), "write_ready": True,
                    "session_id": session["id"], "restores_session_id": saved_session_id, "requires_new_human_approval": True}
        except Exception:
            current = self.store.get("session", started["session_id"], self.owner)
            if current["state"] in {"active", "planned"}:
                self.cancel_form_edit_session(started["session_id"])
            raise

    def _launch(self, kind: str, write_id: str) -> dict:
        from gxp_editor.runtime_package import assert_runtime_resources
        package = Path(__file__).resolve().parents[2]
        assert_runtime_resources(package)
        lock_name = "ui-" + digest(write_id) if kind == "approval" else "connection-window"
        try:
            with file_lock(self.store.root / "locks" / (lock_name + ".lock")):
                pass
        except FormError:
            return {"already_open": True, "window_starting": False, "write_id": write_id or None}
        script = package / "scripts" / ("form_approval_app.py" if kind == "approval" else "form_connection_app.py")
        arguments = [sys.executable, str(script)]
        if kind == "approval":
            arguments += ["--write-id", write_id, "--conversation-id", self.owner]
        self.store.root.joinpath("logs").mkdir(exist_ok=True)
        with (self.store.root / "logs" / "ui.log").open("ab") as output:
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"window_starting": True, "pid": process.pid, "write_id": write_id or None}
