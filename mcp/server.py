from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))

from gxp_editor.service import EditorService
from gxp_editor.compiler import CompilerError
from gxp_editor.telemetry import traced_tool


mcp = FastMCP(
    "gxp-lowcode-editor",
    instructions=(
        "Guarded editing of existing unpublished GXP ActionDesign drafts. "
        "For business editing, create one registered conversation_id per dialogue and supply it on every call. "
        "database_settings is a separate local settings entry, available without editor activation or database configuration; never pass passwords. "
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
@traced_tool
def database_settings(
    operation: Literal["status", "open", "update"] = "status",
    changes: dict[str, Any] | None = None,
    expected_revision: str = "",
) -> dict[str, Any]:
    """Manage local shared Look/Write settings before editor activation or database configuration.

    status reads non-secret local settings without connecting. open launches the
    local settings window for manual edits and hidden password entry. update
    accepts only host, port, database, look_user and write_user explicitly supplied
    by the user; pass the revision from status. It tests the candidate before
    saving and blocks a switch while editor workflows remain active. Never pass
    passwords or connection strings. No conversation, edit session, business-data
    write or approval is created by this local configuration entry.
    """
    from gxp_editor.connection_settings import manage_settings

    return manage_settings(operation, changes, expected_revision)


@mcp.tool()
@traced_tool
def create_conversation(label: str = "") -> dict[str, Any]:
    """Create a dialogue marker once; retain and supply its conversation_id on every later call."""
    return service().create_conversation(label)


@mcp.tool()
@traced_tool
def attach_edit_session(session_id: str, conversation_id: str) -> dict[str, Any]:
    """Take over a shared session marker after its approval window closes; revoke the previous dialogue's access."""
    return service(conversation_id).attach_edit_session(session_id)


@mcp.tool()
@traced_tool
def editor_status(conversation_id: str = "", probe_runtime: bool = False) -> dict[str, Any]:
    """Check readiness and this dialogue's session. Set probe_runtime only for SDK/reference diagnostics."""
    return service(conversation_id).editor_status(probe_runtime=probe_runtime)


@mcp.tool()
@traced_tool
def create_edit_queue(conversation_id: str) -> dict[str, Any]:
    """Create an explicit multi-form/action review queue. No approval or database write occurs."""
    return service(conversation_id).create_edit_queue()


@mcp.tool()
@traced_tool
def get_edit_queue(queue_id: str, conversation_id: str) -> dict[str, Any]:
    """List queued states, cumulative publication_targets and persisted graph-review outcomes. Combine pending completed transactions into one graph review."""
    return service(conversation_id).get_edit_queue(queue_id)


@mcp.tool()
@traced_tool
def begin_edit_session(identifier: str, conversation_id: str = "", queue_id: str = "", handoff_ref: dict[str, str] | None = None,
                       response_detail: Literal["summary", "full"] = "summary") -> dict[str, Any]:
    """Back up one draft; normal entry without a reference returns optional bounded evidence_candidates for this action/environment, and lookup failure does not block editing. Optional handoff_ref={handoff_id,sha256} verifies an edit snapshot before target reuse; diagnosis_only supplies historical findings and still requires current-target inspection. Stale or invalid reference checks create no new session. No authorization or approval is inherited. queue_id allows multiple drafts."""
    return service(conversation_id).begin_edit_session(identifier, queue_id=queue_id, handoff_ref=handoff_ref,
                                                       response_detail=response_detail)


@mcp.tool()
@traced_tool
def begin_reference_repair_session(identifier: str, conversation_id: str = "", queue_id: str = "",
                                  response_detail: Literal["summary", "full"] = "summary") -> dict[str, Any]:
    """Back up one draft, diagnose invalid parameter bindings, and validate its non-reference structure."""
    return service(conversation_id).begin_reference_repair_session(identifier, queue_id=queue_id, response_detail=response_detail)


@mcp.tool()
@traced_tool
def inspect_reference_repairs(
    session_id: str, offset: int = 0, limit: int = 30, conversation_id: str = ""
) -> dict[str, Any]:
    """List invalid bindings and candidates matched by group, exact name, type and visible variable source."""
    return service(conversation_id).inspect_reference_repairs(session_id, offset=offset, limit=limit)


@mcp.tool()
@traced_tool
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
@traced_tool
def find_edit_nodes(
    session_id: str, conversation_id: str = "", kind: Literal["nodes", "groups"] = "nodes", query: str = "",
    group_key: str = "", element_key: str = "", offset: int = 0, limit: int = 30,
) -> dict[str, Any]:
    """Discover exact group/node keys and canvas locations from the owned session backup. Search labels only; no parameters/code. Paginated, read-only and version checked on every call."""
    return service(conversation_id).find_edit_nodes(session_id, kind=kind, query=query, group_key=group_key,
                                                   element_key=element_key, offset=offset, limit=limit)


@mcp.tool()
@traced_tool
def inspect_edit_context(
    session_id: str, targets: list[dict[str, str]], conversation_id: str = "", operation_cursor: int = 0,
    include_context: bool | None = None,
    response_detail: Literal["summary", "full"] = "summary", parameter_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Read backup-bound target summaries and signed old-value guards by default. Use parameter_paths to focus fields, read_edit_value for exact values, or response_detail=full for complete params/parents/native contracts. Explicit include_context preserves legacy full/compact semantics. No writes."""
    return service(conversation_id).inspect_edit_context(session_id, targets, operation_cursor=operation_cursor,
                                                          include_context=include_context, response_detail=response_detail,
                                                          parameter_paths=parameter_paths)


@mcp.tool()
@traced_tool
def read_edit_value(
    session_id: str, group_key: str, node_key: str, path: str, conversation_id: str = "",
    item_key: dict[str, Any] | None = None, offset: int = 0, max_output_bytes: int = 16384,
) -> dict[str, Any]:
    """Read one exact session-bound parameter/row. Large values use canonical JSON text pages with character offsets and a UTF-8 byte budget; keep binding/hash unchanged and follow next_offset. Fragments are not complete old values; use the signed guard_ref for parameter updates. evidence_gaps/fallback are unsigned continuation guidance, not a gate waiver. Every page rechecks ownership/draft/relations/compiler. No writes."""
    return service(conversation_id).read_edit_value(session_id, group_key, node_key, path, item_key=item_key,
                                                    offset=offset, max_output_bytes=max_output_bytes)


@mcp.tool()
@traced_tool
def inspect_component_contract(component_type: str, conversation_id: str = "") -> dict[str, Any]:
    """Read native component events and callback parameters. Page instance identity needs readonly page evidence."""
    return service(conversation_id).inspect_component_contract(component_type)


@mcp.tool()
@traced_tool
def plan_canvas_patch(
    session_id: str, operations: list[dict[str, Any]], conversation_id: str = "",
    review_spec: dict[str, Any] | None = None,
    response_detail: str = "summary",
    generation_mode: Literal["full", "incremental", "auto"] = "full",
) -> dict[str, Any]:
    """Compile a canvas batch and compare original/candidate JS against declarative business expectations.

    review_spec contains schema_version=1, revision, requirements with id/text/source_text/targets,
    scenarios with schema_version=1/cases and optional canvas_assertions. Default scope_policy=strict
    requires targeted requirements and blocks changes outside them. manual_review is explicit.
    Omission reuses previous expectations. Unchanged guarded parameters return no_changes without
    creating a new plan. Fresh empty sessions are released; session_released=false preserves existing work.
    Summary responses retain report identity; use read_plan_review for details
    or response_detail=full. generation_mode defaults to full; incremental preserves unchanged
    saved action functions; auto chooses using actual changed groups and dependency scope.
    Report generation_selection with its reason. Failed checks block approval. No database write.
    """
    try:
        return service(conversation_id).plan_canvas_patch(
            session_id, operations, review_spec=review_spec, response_detail=response_detail,
            generation_mode=generation_mode)
    except CompilerError as exc:
        return _compiler_failure(exc)


def _compiler_failure(exc: CompilerError) -> dict[str, Any]:
    return {"blocked": True, "plan_created": False, "write_executed": False,
            "error": str(exc)[:1000], "error_details": exc.details}


@mcp.tool()
@traced_tool
def read_plan_review(
    plan_id: str, section: str = "summary", offset: int = 0, limit: int = 10,
    item_id: str = "", conversation_id: str = "",
    detail: Literal["summary", "full"] = "summary", field_path: str = "", value_offset: int = 0,
    max_output_bytes: int = 16384,
) -> dict[str, Any]:
    """Read report row summaries without compiling/opening a window. Sections: summary, requirements, changes, scenarios, canvas_checks, spec. item_id selects exact rows; field_path/value_offset reads exact JSON text with character offsets under a byte budget. evidence_gaps/fallback guide bounded continuation without changing review status or signed evidence. detail=full returns exact rows within max_output_bytes; oversized rows provide a summary read descriptor, then use field_path for exact values."""
    return service(conversation_id).read_plan_review(
        plan_id, section=section, offset=offset, limit=limit, item_id=item_id, detail=detail,
        field_path=field_path, value_offset=value_offset, max_output_bytes=max_output_bytes)


@mcp.tool()
@traced_tool
def open_plan_review(plan_id: str, conversation_id: str = "", response_detail: str = "summary") -> dict[str, Any]:
    """Optional standalone preview, only when explicitly requested by the user. Ready plans go directly to prepare_write; failed plans use read_plan_review without a window. Never approves or writes."""
    return service(conversation_id).open_plan_review(plan_id, response_detail=response_detail)


@mcp.tool()
@traced_tool
def prepare_write(plan_id: str, conversation_id: str = "", response_detail: str = "summary") -> dict[str, Any]:
    """Submit this draft's batched edits in one validated plan and one approval transaction/window. Canvas/JS/C# share one UPDATE and one approval; actual additional SQL is approved separately by bound parameters and target row."""
    return service(conversation_id).prepare_write(plan_id, response_detail=response_detail)


@mcp.tool()
@traced_tool
def get_write_status(transaction_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Read save status and next_action/poll_after_ms: starting, waiting for the human, executing or completed. Never respawn a pending window. Completed results include publication_targets and persisted look_graph_review; compare first and ask only about confirmed differences. Never writes."""
    return service(conversation_id).get_write_status(transaction_id)


@mcp.tool()
@traced_tool
def record_graph_review(
    transaction_id: str,
    outcome: Literal["no_difference", "proposed", "declined", "pending_publication", "recorded"],
    conversation_id: str = "", expected_revision: int = 0, proposal_sha256: str = "",
    graph_revision: int | None = None, reconsider_declined: bool = False,
) -> dict[str, Any]:
    """Record an observed graph review outcome for a completed save, using its review revision.

    Store only the concrete proposal's SHA-256, never its body. proposed precedes
    asking once; declined records an explicit refusal of that proposal, never
    silence. pending_publication waits for current published evidence. recorded
    requires an actual successful Look receipt's graph_revision. No outcome grants
    consent or writes the graph. Set reconsider_declined only when the user
    explicitly asks to reconsider that exact declined proposal; ordinary status
    polling or silence never reopens it or supplies graph consent.
    """
    return service(conversation_id).record_graph_review(transaction_id, outcome,
        expected_revision=expected_revision, proposal_sha256=proposal_sha256, graph_revision=graph_revision,
        reconsider_declined=reconsider_declined)


@mcp.tool()
@traced_tool
def reopen_approval(transaction_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Recheck the pending artifact, coherent draft/relations, account/grants and compiler before reopening directly in this conversation. Dead queues use generation-bound launch reservations; unknown/live-stalled queues cannot restart. No external terminal or service is required. No new transaction, approval or execution."""
    return service(conversation_id).reopen_approval(transaction_id)


@mcp.tool()
@traced_tool
def recover_edit_session(session_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Compare an interrupted result with original/candidate snapshots and reconcile local state. verification_scope/current_matches_baseline/candidate describe snapshot evidence; execution_outcome/commit_evidence describe causal proof. executed=false with unknown does not mean never executed; never retry that transaction. No business writes."""
    return service(conversation_id).recover_edit_session(session_id)


@mcp.tool()
@traced_tool
def verify_saved_draft(session_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Verify an unfinished save, or return its completed historical save receipt and look_graph_review without rechecking later edits. A graph review is a comparison hint, never permission to update."""
    return service(conversation_id).verify_saved_draft(session_id)


@mcp.tool()
@traced_tool
def prepare_restore(session_id: str, backup_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Open a new human approval to restore the immutable session backup. A completed verified save must still match the current draft/relations; reacquiring occupancy refuses active conflicts or later saved editing sessions. Approve each actual SQL with its bound parameters and target row, not each component change."""
    return service(conversation_id).prepare_restore(session_id, backup_id)


@mcp.tool()
@traced_tool
def finish_edit_session(session_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Release a verified session and return publication_targets plus look_graph_review. Compare first; ask once to add/update Look relations only when confirmed differences exist, then wait for user consent."""
    return service(conversation_id).finish_edit_session(session_id)


@mcp.tool()
@traced_tool
def inspect_session_cancellation(session_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Preview cancellation and local_release scopes for explicit chat confirmation. Local release retains current data without verifying uncertain saves. Mutates nothing; show the relevant scope and meaning before asking."""
    return service(conversation_id).inspect_session_cancellation(session_id)


@mcp.tool()
@traced_tool
def cancel_unexecuted_session(session_id: str, cancellation_fingerprint: str, user_confirmation: str,
                              conversation_id: str = "") -> dict[str, Any]:
    """Cancel only after explicit user agreement to the inspected scope. Quote that reply; retain data/backups. Changed scope or execution blocks cancellation. Never approves or writes business data."""
    return service(conversation_id).cancel_unexecuted_session(session_id, cancellation_fingerprint, user_confirmation)


@mcp.tool()
@traced_tool
def release_local_session(session_id: str, recovery_fingerprint: str, user_confirmation: str,
                          conversation_id: str = "") -> dict[str, Any]:
    """Only after user explicitly confirms the inspected local_release scope/meaning/fingerprint: retain business data and backups, archive local occupancy as unverified. Live window/executor or changed ownership/snapshot blocks. Never claims save success or writes business data."""
    return service(conversation_id).release_local_session(session_id, recovery_fingerprint, user_confirmation)


@mcp.tool()
@traced_tool
def abort_edit_session(session_id: str, conversation_id: str = "") -> dict[str, Any]:
    """Open manual session handling: cancel unexecuted approval, reopen it, or inspect and recover a finished/interrupted result."""
    return service(conversation_id).abort_edit_session(session_id)


if __name__ == "__main__":
    mcp.run(transport="stdio")
