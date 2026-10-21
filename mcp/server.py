from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))

from gxp_editor.service import EditorService
from gxp_editor.compiler import CompilerError


mcp = FastMCP(
    "gxp-lowcode-editor",
    instructions=(
        "Guarded editing of existing unpublished GXP ActionDesign drafts. "
        "Create one registered conversation_id per dialogue and supply it on every call. "
        "Different dialogues may edit different drafts concurrently. Each draft has one active flow; "
        "use create_edit_queue and queue_id to review multiple drafts in one dialogue/window. "
        "Without a queue each dialogue has one active flow. An omitted conversation_id accesses only legacy flows. "
        "A session marker may be transferred to another dialogue after its approval window "
        "closes; transfer revokes the former dialogue's access. "
        "The MCP creates plans and opens a separate human approval application; "
        "it has no direct write or publish tool and never calls backend editor endpoints, "
        "Redis, or DLL/PDB filesystem operations."
    ),
)


def service(conversation_id: str = "") -> EditorService:
    return EditorService(conversation_id)


@mcp.tool()
def create_conversation(label: str = "") -> dict[str, str]:
    """Create a dialogue marker once; retain and supply its conversation_id on every later call."""
    return service().create_conversation(label)


@mcp.tool()
def attach_edit_session(session_id: str, conversation_id: str) -> dict[str, Any]:
    """Take over a shared session marker after its approval window closes; revoke the previous dialogue's access."""
    return service(conversation_id).attach_edit_session(session_id)


@mcp.tool()
def editor_status(conversation_id: str = "", probe_runtime: bool = False) -> dict[str, Any]:
    """Check readiness and this dialogue's session. Set probe_runtime only for SDK/reference diagnostics."""
    return service(conversation_id).editor_status(probe_runtime=probe_runtime)


@mcp.tool()
def create_edit_queue(conversation_id: str) -> dict[str, Any]:
    """Create an explicit multi-form/action review queue. No approval or database write occurs."""
    return service(conversation_id).create_edit_queue()


@mcp.tool()
def get_edit_queue(queue_id: str, conversation_id: str) -> dict[str, Any]:
    """List this dialogue's queued actions and preparation/save states without approving anything."""
    return service(conversation_id).get_edit_queue(queue_id)


@mcp.tool()
def begin_edit_session(identifier: str, conversation_id: str = "", queue_id: str = "") -> dict[str, Any]:
    """Back up one draft. An explicit queue_id allows multiple distinct drafts in this dialogue's review window."""
    return service(conversation_id).begin_edit_session(identifier, queue_id=queue_id)


@mcp.tool()
def begin_reference_repair_session(identifier: str, conversation_id: str = "", queue_id: str = "") -> dict[str, Any]:
    """Back up one draft, diagnose invalid parameter bindings, and validate its non-reference structure."""
    return service(conversation_id).begin_reference_repair_session(identifier, queue_id=queue_id)


@mcp.tool()
def inspect_reference_repairs(
    session_id: str, offset: int = 0, limit: int = 30, conversation_id: str = ""
) -> dict[str, Any]:
    """List invalid bindings and candidates matched by group, exact name, type and visible variable source."""
    return service(conversation_id).inspect_reference_repairs(session_id, offset=offset, limit=limit)


@mcp.tool()
def plan_reference_repairs(
    session_id: str, choices: list[dict[str, str]] | None = None, conversation_id: str = "",
    review_spec: dict[str, Any] | None = None,
    response_detail: str = "summary",
) -> dict[str, Any]:
    """Repair unique bindings with confirmed choices and compile the whole candidate. Return bounded review evidence by default."""
    try:
        return service(conversation_id).plan_reference_repairs(
            session_id, choices, review_spec=review_spec, response_detail=response_detail)
    except CompilerError as exc:
        return _compiler_failure(exc)


@mcp.tool()
def inspect_edit_context(
    session_id: str, targets: list[dict[str, str]], conversation_id: str = "",
) -> dict[str, Any]:
    """Read backup-bound parameters, canvas lines, parents, visible variables and native contracts for 1–10 exact group_key/node_key targets. No writes."""
    return service(conversation_id).inspect_edit_context(session_id, targets)


@mcp.tool()
def inspect_component_contract(component_type: str, conversation_id: str = "") -> dict[str, Any]:
    """Read native component events and callback parameters. Page instance identity needs readonly page evidence."""
    return service(conversation_id).inspect_component_contract(component_type)


@mcp.tool()
def plan_canvas_patch(
    session_id: str, operations: list[dict[str, Any]], conversation_id: str = "",
    review_spec: dict[str, Any] | None = None,
    response_detail: str = "summary",
) -> dict[str, Any]:
    """Compile a canvas batch and compare original/candidate JS against declarative business expectations.

    review_spec contains schema_version=1, revision, requirements with id/text/source_text/targets,
    scenarios with schema_version=1/cases and optional canvas_assertions. Default scope_policy=strict
    requires targeted requirements and blocks changes outside them. manual_review is explicit.
    Omission reuses previous expectations. Unchanged guarded parameters return no_changes without
    creating a new plan. Summary responses retain report identity; use read_plan_review for details
    or response_detail=full. Failed checks block approval. No database write.
    """
    try:
        return service(conversation_id).plan_canvas_patch(
            session_id, operations, review_spec=review_spec, response_detail=response_detail)
    except CompilerError as exc:
        return _compiler_failure(exc)


def _compiler_failure(exc: CompilerError) -> dict[str, Any]:
    return {"blocked": True, "plan_created": False, "write_executed": False,
            "error": str(exc)[:1000], "error_details": exc.details}


@mcp.tool()
def read_plan_review(
    plan_id: str, section: str = "summary", offset: int = 0, limit: int = 10,
    item_id: str = "", conversation_id: str = "",
) -> dict[str, Any]:
    """Read bound report evidence without compiling or opening a window. Sections: summary, requirements, changes, scenarios, canvas_checks, spec. Pages contain at most 20 complete records; item_id filters exact IDs."""
    return service(conversation_id).read_plan_review(
        plan_id, section=section, offset=offset, limit=limit, item_id=item_id)


@mcp.tool()
def open_plan_review(plan_id: str, conversation_id: str = "", response_detail: str = "summary") -> dict[str, Any]:
    """Optional standalone preview, only when explicitly requested by the user. Ready plans go directly to prepare_write; failed plans use read_plan_review without a window. Never approves or writes."""
    return service(conversation_id).open_plan_review(plan_id, response_detail=response_detail)


@mcp.tool()
def prepare_write(plan_id: str, conversation_id: str = "", response_detail: str = "summary") -> dict[str, Any]:
    """Freeze a validated plan and open the separate per-statement human approval application."""
    return service(conversation_id).prepare_write(plan_id, response_detail=response_detail)


@mcp.tool()
def get_write_status(transaction_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Read approval and database execution status. This tool never writes."""
    return service(conversation_id).get_write_status(transaction_id)


@mcp.tool()
def reopen_approval(transaction_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Reopen an existing unexecuted approval window without creating or executing a transaction."""
    return service(conversation_id).reopen_approval(transaction_id)


@mcp.tool()
def recover_edit_session(session_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Check an interrupted/uncertain result against database hashes and reconcile local state; no business writes."""
    return service(conversation_id).recover_edit_session(session_id)


@mcp.tool()
def verify_saved_draft(session_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Verify an unfinished save, or return its completed historical save receipt without rechecking later edits."""
    return service(conversation_id).verify_saved_draft(session_id)


@mcp.tool()
def prepare_restore(session_id: str, backup_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Open a new per-statement approval transaction that restores the immutable session backup."""
    return service(conversation_id).prepare_restore(session_id, backup_id)


@mcp.tool()
def finish_edit_session(session_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Release a verified session and remind the user to inspect and publish manually."""
    return service(conversation_id).finish_edit_session(session_id)


@mcp.tool()
def abort_edit_session(session_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Open manual session handling: cancel unexecuted approval, reopen it, or inspect and recover a finished/interrupted result."""
    return service(conversation_id).abort_edit_session(session_id)


if __name__ == "__main__":
    mcp.run(transport="stdio")
