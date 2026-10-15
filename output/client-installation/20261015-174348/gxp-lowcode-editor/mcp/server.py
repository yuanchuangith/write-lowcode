from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))

from gxp_editor.service import EditorService


mcp = FastMCP(
    "gxp-lowcode-editor",
    instructions=(
        "Guarded editing of existing unpublished GXP ActionDesign drafts. "
        "Create one registered conversation_id per dialogue and supply it on every call. "
        "Different dialogues may edit different drafts concurrently; each dialogue and draft "
        "has at most one active flow. An omitted conversation_id accesses only legacy flows. "
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
def editor_status(conversation_id: str = "") -> dict[str, Any]:
    """Check readiness and only this dialogue's active session; an omitted marker accesses legacy flows."""
    return service(conversation_id).editor_status()


@mcp.tool()
def begin_edit_session(identifier: str, conversation_id: str = "") -> dict[str, Any]:
    """Back up and lock one draft for this dialogue; other dialogues may edit different drafts concurrently."""
    return service(conversation_id).begin_edit_session(identifier)


@mcp.tool()
def begin_reference_repair_session(identifier: str, conversation_id: str = "") -> dict[str, Any]:
    """Back up one draft, diagnose invalid parameter bindings, and validate its non-reference structure."""
    return service(conversation_id).begin_reference_repair_session(identifier)


@mcp.tool()
def inspect_reference_repairs(
    session_id: str, offset: int = 0, limit: int = 30, conversation_id: str = ""
) -> dict[str, Any]:
    """List invalid bindings and candidates matched by group, exact name, type and visible variable source."""
    return service(conversation_id).inspect_reference_repairs(session_id, offset=offset, limit=limit)


@mcp.tool()
def plan_reference_repairs(
    session_id: str, choices: list[dict[str, str]] | None = None, conversation_id: str = ""
) -> dict[str, Any]:
    """Batch repair unique bindings; supply repair_id/candidate_id only for confirmed ambiguities. Compile the whole candidate before creating a plan."""
    return service(conversation_id).plan_reference_repairs(session_id, choices)


@mcp.tool()
def plan_canvas_patch(
    session_id: str, operations: list[dict[str, Any]], conversation_id: str = ""
) -> dict[str, Any]:
    """Compile a structured canvas patch without writing to the database."""
    return service(conversation_id).plan_canvas_patch(session_id, operations)


@mcp.tool()
def prepare_write(plan_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Freeze a validated plan and open the separate per-statement human approval application."""
    return service(conversation_id).prepare_write(plan_id)


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
    """Re-read the draft and verify it matches the approved transaction hash."""
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
