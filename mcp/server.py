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
        "Guarded editing of one existing unpublished GXP ActionDesign draft. "
        "The MCP creates plans and opens a separate human approval application; "
        "it has no direct write or publish tool and never calls backend editor endpoints, "
        "Redis, or DLL/PDB filesystem operations."
    ),
)


def service() -> EditorService:
    return EditorService()


@mcp.tool()
def editor_status() -> dict[str, Any]:
    """Check planning/write readiness, indexes, account safety, compilers, and active session."""
    return service().editor_status()


@mcp.tool()
def begin_edit_session(identifier: str) -> dict[str, Any]:
    """Back up and lock exactly one existing active unpublished draft by action code or RefId."""
    return service().begin_edit_session(identifier)


@mcp.tool()
def plan_canvas_patch(session_id: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Compile a structured canvas patch without writing to the database."""
    return service().plan_canvas_patch(session_id, operations)


@mcp.tool()
def prepare_write(plan_id: str) -> dict[str, Any]:
    """Freeze a validated plan and open the separate per-statement human approval application."""
    return service().prepare_write(plan_id)


@mcp.tool()
def get_write_status(transaction_id: str) -> dict[str, Any]:
    """Read approval and database execution status. This tool never writes."""
    return service().get_write_status(transaction_id)


@mcp.tool()
def verify_saved_draft(session_id: str) -> dict[str, Any]:
    """Re-read the draft and verify it matches the approved transaction hash."""
    return service().verify_saved_draft(session_id)


@mcp.tool()
def prepare_restore(session_id: str, backup_id: str) -> dict[str, Any]:
    """Open a new per-statement approval transaction that restores the immutable session backup."""
    return service().prepare_restore(session_id, backup_id)


@mcp.tool()
def finish_edit_session(session_id: str) -> dict[str, Any]:
    """Release a verified session and remind the user to inspect and publish manually."""
    return service().finish_edit_session(session_id)


@mcp.tool()
def abort_edit_session(session_id: str) -> dict[str, Any]:
    """Abort only before any write approval transaction begins."""
    return service().abort_edit_session(session_id)


if __name__ == "__main__":
    mcp.run(transport="stdio")
