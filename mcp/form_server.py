from __future__ import annotations

import functools
import sys
import time
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gxp_form_editor.common import DOMAIN, FormError
from gxp_form_editor.service import FormService
from gxp_project_context.context import ProjectError, request_context

mcp = FastMCP("gxp-form-config-editor", instructions=(
    "Independent editing of existing unpublished form components. Never use ActionDesign sessions or approvals. "
    "Create one form conversation and supply it on every business call. Find exact existing components and use "
    "native property contracts with complete old-value guards. No component creation/deletion/reparenting, model "
    "entity changes, arbitrary JSON, code, URL, SQL or publish tools. Missing native draft/concurrency/association "
    "evidence keeps write_ready=false. prepare_form_write only opens the local human review application; only its "
    "human can approve and confirm execution. Recover uncertain saves without replaying requests."
))


class PageTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    environment: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=500)
    app_id: str = Field(min_length=1, max_length=128)
    site_out_id: str = Field(min_length=1, max_length=128)
    page_id: str = Field(min_length=1, max_length=128)
    route: str = Field(min_length=1, max_length=200)
    data_version_id: str = Field(min_length=1, max_length=128)
    platform: Literal["web", "wap"] = "web"


class FormOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["set_property", "set_default", "set_layout", "set_binding", "set_filter"]
    component_id: str = Field(min_length=1, max_length=256)
    expected_component_type: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=1000)
    expected_old_value: Any
    value: Any
    expected_old_exists: bool = True
    platform: Literal["web", "wap"] | None = None


def exposed(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        started = time.monotonic()
        try:
            with request_context(role="write"):
                result = function(*args, **kwargs)
        except FormError as error:
            result = {"domain": DOMAIN, "error_code": error.code, "message": str(error), "details": error.details}
        except ProjectError as error:
            result = {"domain": DOMAIN, "error_code": error.code, "message": str(error), "details": error.details}
        except Exception:
            result = {"domain": DOMAIN, "error_code": "form_internal_error", "message": "表单编辑请求失败；未自动重试保存。请核对本机设置或恢复状态。"}
        return {**result, "total_elapsed_ms": round((time.monotonic() - started) * 1000, 2)}
    return call


@mcp.tool()
@exposed
def create_form_conversation(label: str = "") -> dict[str, Any]:
    """Register an isolated form dialogue; no platform request or approval occurs."""
    return FormService().create_form_conversation(label)


@mcp.tool()
@exposed
def form_editor_status() -> dict[str, Any]:
    """Report read/write readiness without accepting credentials or modifying business data."""
    return FormService().form_editor_status()


@mcp.tool()
@exposed
def form_connection_settings(operation: Literal["status", "open"] = "status") -> dict[str, Any]:
    """Read non-secret setup or open the independent local platform login window."""
    return FormService().form_connection_settings(operation)


@mcp.tool()
@exposed
def begin_form_edit_session(target: PageTarget, conversation_id: str) -> dict[str, Any]:
    """Read the exact native draft and back it up. Missing write evidence permits only preview."""
    return FormService(conversation_id).begin_form_edit_session(target.model_dump())


@mcp.tool()
@exposed
def find_form_components(session_id: str, conversation_id: str, query: str = "", offset: int = 0, limit: int = 30) -> dict[str, Any]:
    """Search the existing component tree without returning whole-page configuration."""
    return FormService(conversation_id).find_form_components(session_id, query, offset, limit)


@mcp.tool()
@exposed
def inspect_form_component(session_id: str, component_id: str, conversation_id: str,
                           response_detail: Literal["summary", "full"] = "summary") -> dict[str, Any]:
    """Read one component and its version-bound native editable property contract."""
    return FormService(conversation_id).inspect_form_component(session_id, component_id, response_detail)


@mcp.tool()
@exposed
def inspect_form_resource(session_id: str, kind: Literal["datasets", "dictionaries"], resource_id: str,
                           conversation_id: str) -> dict[str, Any]:
    """Read native resource metadata and its version before choosing an existing binding."""
    return FormService(conversation_id).inspect_form_resource(session_id, kind, resource_id)


@mcp.tool()
@exposed
def read_form_value(session_id: str, component_id: str, path: str, conversation_id: str,
                    offset: int = 0, max_chars: int = 8192) -> dict[str, Any]:
    """Read an exact old value in bounded JSON text pages; offsets are Unicode characters."""
    return FormService(conversation_id).read_form_value(session_id, component_id, path, offset, max_chars)


@mcp.tool()
@exposed
def plan_form_patch(session_id: str, operations: list[FormOperation], source_text: str, conversation_id: str) -> dict[str, Any]:
    """Build a guarded native form patch. Does not approve or save; unsupported setters remain read-only."""
    values = [value.model_dump(exclude={"platform"} if value.platform is None else set()) for value in operations]
    return FormService(conversation_id).plan_form_patch(session_id, values, source_text)


@mcp.tool()
@exposed
def read_form_plan(plan_id: str, conversation_id: str, section: Literal["changes", "requests", "warnings", "operations"] = "changes",
                   offset: int = 0, limit: int = 20, value_path: str = "", value_offset: int = 0, max_chars: int = 8192) -> dict[str, Any]:
    """Read selected before/after changes or non-secret actual HTTP requests."""
    return FormService(conversation_id).read_form_plan(plan_id, section, offset, limit, value_path, value_offset, max_chars)


@mcp.tool()
@exposed
def prepare_form_write(plan_id: str, conversation_id: str) -> dict[str, Any]:
    """Open the native form-only human approval application after all save checks pass."""
    return FormService(conversation_id).prepare_form_write(plan_id)


@mcp.tool()
@exposed
def get_form_write_status(write_id: str, conversation_id: str) -> dict[str, Any]:
    """Completion requires verified=true and session_released=true; saving is never publication."""
    return FormService(conversation_id).get_form_write_status(write_id)


@mcp.tool()
@exposed
def cancel_form_edit_session(session_id: str, conversation_id: str) -> dict[str, Any]:
    """Cancel only an explicitly requested, unexecuted form workflow; keep backup/history."""
    return FormService(conversation_id).cancel_form_edit_session(session_id)


@mcp.tool()
@exposed
def recover_form_edit_session(session_id: str, conversation_id: str) -> dict[str, Any]:
    """Reconcile an uncertain save by reading the native draft and associations; never replay writes."""
    return FormService(conversation_id).recover_form_edit_session(session_id)


@mcp.tool()
@exposed
def plan_form_restore(saved_session_id: str, conversation_id: str) -> dict[str, Any]:
    """Create a new guarded inverse plan from a verified save; requires fresh human approval."""
    return FormService(conversation_id).plan_form_restore(saved_session_id)


if __name__ == "__main__":
    mcp.run(transport="stdio")
