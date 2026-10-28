"""Transport-only next steps; these hints never authorize an editor mutation."""
from __future__ import annotations


def write_status_guidance(transaction, session, startup, *, artifact_valid=True, conversation_id=None):
    state, session_state = transaction["state"], session["state"]
    startup_state = startup.get("startup_status")
    if state == "saved" and session_state == "finished" and session.get("transaction_id") == transaction["id"]:
        phase, action, interval = "completed", "report_completion", None
    elif session_state in {"closed_unverified", "aborted", "cancelled"} or state in {"cancelled", "failed_no_write", "rolled_back", "closed_unverified"}:
        phase, action, interval = "stopped", "report_outcome", None
    elif state in {"failed", "saved_state_pending"}:
        phase, action, interval = "uncertain", "recover_edit_session", None
    elif state == "saved" and session.get("transaction_id") != transaction["id"]:
        phase, action, interval = "historical", "inspect_current_session", None
    elif state == "saved":
        phase, action, interval = "saved_unfinished", ("finish_edit_session" if session_state == "verified" else "verify_saved_draft"), None
    elif state == "executing" or startup.get("window_executing"):
        phase, action, interval = "executing", "get_write_status", 2000
        if startup_state in {"failed", "closed", "heartbeat_timeout"}:
            phase, action, interval = "uncertain", "recover_edit_session", None
    elif not artifact_valid:
        phase, action, interval = "invalid_artifact", "inspect_session_cancellation", None
    elif startup.get("window_starting") or startup_state == "starting":
        phase, action, interval = "starting", "get_write_status", 1000
    elif startup_state in {"startup_timeout", "heartbeat_timeout", "failed"}:
        phase, action, interval = "window_problem", "inspect_window_failure", None
    elif startup_state == "closed":
        phase, action, interval = "window_closed", "wait_for_continuation", None
    elif startup.get("window_opened") or startup_state in {"ready", "already_open"}:
        phase, action, interval = "awaiting_human", "wait_for_user", 10000
    else:
        phase, action, interval = "window_unknown", "inspect_window_failure", None
    # Keep the established action names, but make inspection/recovery hints
    # callable without inventing a tool or dropping the dialogue's ownership.
    marker = session.get("conversation_id", "") if conversation_id is None else conversation_id
    session_id = session.get("id") or transaction.get("session_id")
    next_read = None
    if action == "get_write_status":
        next_read = {"tool": action, "arguments": {"transaction_id": transaction["id"], "conversation_id": marker}}
    elif action == "inspect_current_session":
        next_read = {"tool": "editor_status", "arguments": {"conversation_id": marker}}
    elif action in {"recover_edit_session", "verify_saved_draft", "finish_edit_session", "inspect_session_cancellation", "inspect_window_failure"} and session_id:
        next_read = {"tool": "inspect_session_cancellation" if action == "inspect_window_failure" else action,
                     "arguments": {"session_id": session_id, "conversation_id": marker}}
    return {"workflow_phase": phase, "next_action": action, "next_read": next_read, "poll_after_ms": interval,
            "progress_update_after_ms": 30000,
            "polling_policy": "Report progress at least every 30 seconds while working. When awaiting_human, report and yield; poll_after_ms is only a minimum delay for a requested recheck, not an instruction to keep polling. Human approval has no deadline; never respawn or approve. Diagnose execution without progress for 30 seconds; elapsed time never proves failure or permits clearing locks.",
            "startup_check_budget_ms": 30000 if phase == "starting" else None}
