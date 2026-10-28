# Session workflows

Read only the section for the current session state. These procedures retain the
compiler, backup, ownership, concurrency and human approval gates in SKILL.md.
A previously authorized same-dialogue, same-draft workflow may continue without
repeating explicit skill invocation; no section authorizes a new edit target.

## Conversation editing and approval

Keep the existing dialogue workflow: register the conversation, back up and
inspect the draft, prepare and review one plan, then call `prepare_write` to open
the human approval window directly. The user approves the actual SQL and confirms
saving; verify the result and release the session before reporting completion.
No separate terminal, UI host or background-service setup is a prerequisite.
Keep the tool connection available while review is pending. Closing a window
does not itself cancel its pending transaction or authorize another save.

## Blocked session entry

Without an explicitly requested queue, a dialogue owns at most one active
session. A different target in the same dialogue returns `blocked=true` and
opens a local session handling window before backup or compiler work. Another
dialogue's exclusive target reports the conflict without opening its window.
Closing the handling window retains its session; never treat it as cancellation.
`abort_edit_session` requests this window and returns `aborted=false`; use its
startup result to distinguish opened, still starting and failed. After the
human resolves it, recheck `editor_status.active_session` before retrying entry.
Cancellation invalidates unexecuted plans and approvals while retaining backup
and audit records. For a chat-authorized cancellation, use the procedure below;
the handling window and explicitly confirmed local release remain available.
Never forcibly clear live execution or silently discard an uncertain result.

## Chat-authorized cancellation of unexecuted work

When the user wants AI to cancel an old unexecuted workflow, call
`inspect_session_cancellation(session_id, conversation_id)`. Show the action name
and code, the exact plan/approval IDs and the effect: invalidate that review,
release the local session, retain current business data, backups and history.
If `cancellable=false`, explain the returned reason and use recovery, the
handling window or the separate local-release procedure below. Do not fabricate
a cancellation fingerprint or describe local release as proof that no write occurred.

After presenting this concrete scope, ask whether AI may cancel it. Only an
explicit user reply agreeing to that scope authorizes
`cancel_unexecuted_session(session_id, cancellation_fingerprint, user_confirmation, conversation_id)`.
Use the inspected fingerprint and quote the user's actual reply. Permission to
add this capability, a general preference, silence, a timeout or approval of a
business change is not confirmation to cancel a particular workflow. An existing
explicit reply covering the displayed scope need not be requested again.

The tool atomically rechecks ownership, revision, plan and transaction states,
serializes against execution, records the confirmation and invalidates pending
review. An open approval window cannot execute the cancelled transaction. A new
plan/approval, handoff or execution invalidates the inspected scope; show the new
scope and obtain a new confirmation. Saved, restore and uncertain flows remain
outside this tool. Never call local state/closure helpers as a substitute.
Cancellation neither approves nor executes business SQL; future saves still
require the complete compiler checks and the human approval application.

## Chat-authorized local release

When verification is unavailable, the user may choose to keep the current
business data and release local occupancy without establishing whether the old
save succeeded. Call `inspect_session_cancellation(session_id, conversation_id)`
and inspect its separate `local_release` object: `available`, `fingerprint`,
`requires_user_confirmation`, `reason` and `meaning`. Show the action, session,
exact plan/approval scope and returned meaning before requesting agreement.

Only an explicit reply agreeing to that displayed local-release scope authorizes
`release_local_session(session_id, recovery_fingerprint, user_confirmation, conversation_id)`.
Use `local_release.fingerprint` as `recovery_fingerprint` and quote the actual
reply. An existing explicit agreement covering that scope need not be requested
again. Permission to cancel an unexecuted review, repair the plugin or save a
business change does not authorize discarding an unverified workflow.

The tool archives only local state. It retains current business data, backups
and audit history, invalidates pending plans/approvals and returns an unverified
result. It neither writes/restores business data nor proves that the old save
succeeded or never happened. Never report local release as verified saving or
publication. Subsequent editing must reread the draft, regenerate its plan and
obtain new human approval; old approvals cannot be reused.

Close this workflow's approval programs first. Window/executor locks, the legacy
process check, ownership and exact inspected snapshot still guard release.
If `available=false` or release is blocked, report the reason and resolve that
condition; a timeout never supplies permission or proves execution stopped.
If the scope changes, inspect and present the changed scope before obtaining
new agreement. Never call local closure/state helpers as a substitute for the
dedicated tool or automatically release a session after an error.

## Explicit multi-action queues

For an explicitly authorized request covering several identified actions, call
`create_edit_queue(conversation_id)` once and pass `queue_id` to each session
entry. Keep expectations, backup, plan and transaction separate for each draft.
`prepare_write` opens the queue window for the first ready action; later ready
actions join that window automatically. Do not reopen each queued action.
Use `get_edit_queue` to track all items, including preparation still in progress.
For each draft, batch its confirmed operations into one plan and one approval
transaction/window, not one submission per component. The main draft-row UPDATE
contains data, JS and C# together and is approved once. Only actual additional
SQL is approved separately, by SQL plus bound parameters/target row even when
templates match. The human's final save confirmation executes those approved
statements rather than approving them again. Approvals are never inherited or
automatically granted to the whole SQL list. These are independent transactions: a later failure
does not roll back earlier saves. An unknown outcome pauses the queue for
recovery. Closing the window retains queued work; reopening neither signs nor
writes. A transferred session leaves its original queue and opens independently.

## Batch repair of legacy parameter references

When the explicitly requested work is to repair obsolete structured parameter
bindings, use `begin_reference_repair_session(identifier)` instead of ordinary
session entry. It backs up the exact original draft before scanning references.
The original need not pass variable-binding validation. An inspection-only copy
neutralizes only the reported input/output/global/local variable references so
the native compiler can still reject unknown elements, bad languages, duplicate
keys and broken nesting, while reporting action-reference warnings. This diagnostic projection is
never compiled to executable code, planned, approved or saved. A repair session
does not imply that the original canvas is valid.

1. Check the immutable backup, compiler binding, draft identity and exclusive
   draft ownership as usual. Outside an explicit queue, this dialogue cannot own
   a second active session; queue sessions pass the same `queue_id` on entry.
   Other dialogues may work on other drafts.
2. Call `inspect_reference_repairs(session_id, offset, limit)` to review all
   invalid bindings. Reports include group, row, node, field path, neighboring
   nodes, before value, compatible candidates and exact variable provenance.
3. Exact name/expression plus matching type and a unique visible source can be
   repaired automatically. Inputs are searched only in the same action group;
   local outputs must precede the use and be visible according to ParamInput's
   scope rules. Never replace all `oldSource` occurrences with a same-name input:
   use the declared local output when that is the unique compatible source.
4. Ask the user only about ambiguous candidates. Submit their choices as
   `{repair_id, candidate_id}` records to `plan_reference_repairs`. Do not infer
   confirmation or supply arbitrary replacement expressions. Unknown types,
   duplicate sources and input/local/global name collisions require review.
   No candidate, incompatible types, property selections or unsupported bindings
   remain unresolved and block a plan; explain the exact field needing manual work.
5. `plan_reference_repairs` first requires every binding to resolve, then uses the
   complete repaired canvas as the real compiler input, regenerates affected
   native nodes and all JS/C#/relations, and revalidates the whole result. Supply
   `review_spec` under the same business-review protocol; unchanged behavior can
   be an acceptance rule. Compilation failure creates no completed plan. A business
   mismatch returns a blocked plan; inspect its summary and `read_plan_review` without opening a preview or approval window. Ordinary
   `plan_canvas_patch` cannot operate on a repair session.
   Keep the MCP's default summary response; use `read_plan_review` for selected
   issues rather than repeating the complete repair report in the model context.
6. Open the validated repair approval directly with one `prepare_write` for the
   draft's batch. Human approval covers each actual SQL statement with its bound
   parameters/target row, not each repair or component; the main draft UPDATE is
   approved once. Retain final execution confirmation, saved-draft verification
   and finish steps. `prepare_write` rechecks
   reference provenance and the complete candidate; the approval window includes
   a signed, read-only “引用修复来源” tab. The bindings explicitly selected for repair
   must resolve; other static variable/action diagnostics are advisory.
   Publication remains manual.

## Restore workflow

- Call `prepare_restore(session_id, backup_id, conversation_id)` with that session's immutable backup identifier.
- A completed verified save can start a new restore approval only while the current
  unpublished draft and complete relations still match its saved receipt. The tool
  atomically reacquires local occupancy; an active conflicting session or a later
  saved editing session blocks it. Inspect the current draft rather than bypassing
  this guard or changing the original backup/baseline.
- Restoration is a new approval transaction; never describe it as automatic rollback.
- Poll get_write_status, then call verify_saved_draft.
- Finish only when the restored hashes match the immutable backup.
- A rejected restore preparation creates no executable approval. Cancelling an
  unexecuted restore retains the prior saved transaction for verification and finish.

## Session recovery

- Baseline failures include `draft_baseline_changed`, `draft_relations_changed`
  or `baseline_evidence_unavailable`, with a versioned comparison and minimum
  next step. `revision_only_changed` still blocks old CAS conditions. Never
  call this backup expiration, refresh the old baseline, edit its timestamp or
  carry old approvals into a new plan. Use the inspected, explicitly confirmed
  cancellation procedure before starting from the current draft.
- Reopening checks the actual current draft and complete relations in one
  consistent read view, original account/grants, current compiler binding and
  local ownership/pointers. New transactions bind the authenticated principal
  and database scope in signed `database_safety`; old valid transactions report
  `principal_verification=legacy_grants_only` without being retroactively signed.
  Unknown required evidence disables reopening; it is not proof of no change.
- `plugin_runtime_path_missing` and `plugin_runtime_resource_changed` identify
  a broken instance resource and require a connection to an intact runtime.
  Approval/recovery windows open directly from the active conversation. Do not
  ask the user to start a host in another terminal. If an older instance reports
  `ui_host_required`, report that incompatible instance behavior while retaining
  the pending flow and let the user decide the next step. Do not reinstall,
  update the plugin, modify the installed cache or restart the MCP without the
  user's separate instruction.
  Retain the pending transaction; repeated `prepare_write` is not recovery.
- Each startup has its own journal. Launcher PID/exit and claimed UI PID/exit
  are separate evidence. `process_state=unknown` and a missing heartbeat cannot
  establish death, non-execution or a specific terminating process. `ready`
  means visible/event-loop ready; use `review_ready` and `preview_pending` for
  review readiness. A late old event does not describe the new startup.

- New approval saves verify the committed draft and relations and release the
  session in the save worker before showing a publication prompt. Completed
  sessions retain a `saved_verification` receipt. Status/verification calls reuse
  that historical result without comparing it to code regenerated by a later
  platform save or publication. This does not verify those later changes.
- A `saved` transaction without `verified=true` and `session_released=true` still
  needs verification and finish, including saves made by older approval apps.
  Do not prompt platform saving/publication while that session is unfinished.
- Approval/handling window startup uses bounded receipts. Follow `window_starting`,
  `startup_error` and the returned handling alternatives from `reopen_approval`,
  `get_write_status` or `abort_edit_session`; starting is not opened or failed.
  While startup is pending, check status without launching another window.
- `startup_timeout` means no visible-window receipt arrived within 30 seconds;
  it does not prove the process stopped or that execution never began. Use the
  returned process ID and log for diagnosis without repeatedly spawning windows.
  An inspected unexecuted review can still use confirmed chat cancellation;
  uncertain results retain the live-process and executor protections until the
  original process is closed or finishes. Never clear those locks to hide a hang.
- Explicit `reopen_approval` can recover a stalled single-action window only after
  its startup ID, actual UI process creation identity and current unexecuted state
  are rechecked under the execution lock. It reopens the existing pending flow;
  it neither approves, executes nor releases the session. Recovery refusal returns
  handling alternatives. Queue processes are not force-stopped: corrupted
  unexecuted candidates are isolated and retryable while healthy entries continue;
  uncertain execution stops the queue. Registry display metadata loads after the
  first visible screen and does not replace artifact validation.
- If startup reports a failure and the pending artifact remains valid, retry once
  with `reopen_approval` and the returned transaction ID without another opening
  confirmation. Report a repeated failure and use the returned alternatives
  instead of looping. A missing/corrupt/incompatible approval artifact cannot be
  repaired by reopening it: inspect cancellation or local release, explain the
  concrete scope and obtain the corresponding confirmation before acting.
- If the user intentionally closes the window, reopen that pending review only after a continuation request, without asking for another opening confirmation. Reopening neither approves nor executes statements. Do not repeatedly reopen a dismissed window.
- Expired approvals can be renewed by the user in the same window; execution
  continues to verify every signature and expiry.
- An unexecuted restore can be cancelled in the session handling window. It
  returns to the original saved transaction so verification or restore can retry.
- For `failed`, `saved_state_pending`, or an interrupted `executing` transaction,
  use `recover_edit_session` to compare the current design and relations with the
  approved candidate and signed pre-write relation hash. Recovery never modifies
  business data. A match to the candidate becomes verified; call
  `finish_edit_session`. A match to the original releases the failed session.
- Snapshot verification and execution attribution remain separate through
  status, verification, finish and history. A candidate match can have
  `verified=true`, `executed=false`, `execution_outcome=unknown` and
  `commit_evidence=snapshot_match_only`. Explain that the current result matches,
  while this transaction's commit is unproven. Never interpret false as never
  executed or retry the original transaction. Historical receipts explicitly
  report that the current draft was not reread; do not promise it is unchanged.
- A positively identified dead queue can reopen its remaining pending items
  after every required check. Completed entries are displayed, never repeated.
  A reserved generation must match its exact exited startup before replacement;
  timeout alone cannot release it. A live stalled queue is not automatically
  terminated. Membership/pointer changes during launch reject the old preflight.
- The executor and recovery share an OS lock. Recovery cannot release a live
  executor. Old executing records without this protocol cannot be assumed dead.
- If neither snapshot matches, use the handling window's explicit human choice
  or the chat-authorized local-release procedure to retain current database data
  and close the session. This archives the mismatch without asserting execution
  success or overwriting data.
- If configuration, database access, a missing/corrupt approval file, a damaged
  local baseline, or a legacy execution record prevents verification, either open
  `abort_edit_session` with this dialogue's marker or use chat-authorized local
  release. The window's “保留数据并释放本地流程” and the dedicated release tool
  archive an unverified flow without configuration, compiler or database reads.
  Both retain backups/audit and require explicit agreement and stopped approval
  programs. If window startup also fails, use the inspected local-release scope;
  do not send the user repeatedly to a window that never opened. Never call a
  local closure helper or treat a launch request as completed release.
- When entry is blocked, the session handling window is also available for saved,
  verified, pending restore, and uncertain sessions; do not repeatedly retry entry.

## Approval status and polling

Use `get_write_status` for an existing transaction and follow its `next_action`
and `poll_after_ms`. When supplied, `next_read.tool` and `next_read.arguments`
identify the concrete next call, including this dialogue's `conversation_id`;
abstract action names such as `inspect_window_failure` are not tool names.
A tool receipt describes the current phase; it does not
authorize another approval window, SQL approval or execution. Query only this
dialogue's transaction. Keep waits bounded so a progress update can be sent
within 30 seconds when work is still active.

| Observed phase | Next step |
| --- | --- |
| `window_starting` | Startup is pending. Wait for the suggested interval, then read status for the same transaction. Do not call `prepare_write` or reopen a second window. A timeout does not prove the process stopped. |
| Window ready, awaiting human approval/save confirmation | Tell the user the formal window is waiting for their action. Yield while waiting instead of continuous polling. Silence is neither approval nor a reason to reopen the window. |
| `executing` | Wait for the bounded suggested interval and query status. Keep executor protection intact; do not prepare, cancel, recover or launch another write while it is live. |
| Saved but not verified/released | Follow the returned verification/recovery action. A successful SQL result alone does not complete the workflow. |
| `verified=true` and `session_released=true` | Stop transaction polling, report the verified save/publication targets and handle the stored graph-review state. |
| Startup failure, interrupted execution or uncertain result | Follow the matching recovery branch above. Retry startup once only when its artifact is valid; repeated failures use the returned alternatives. |

A failure explicitly proven to precede the execution claim leaves an unexecuted
approval: inspect/cancel that stale approval under the existing confirmation
rules and replan. Do not send it to execution-result recovery. Unknown execution
still requires recovery. A handling window reporting an unverified outcome keeps
the session; only `session_released=true` confirms release. Its slow operations
run in the background; do not repeat an operation while it is pending.

If an older response lacks these hints, distinguish its transaction state and
startup receipt using the same rules. A human review state needs no rapid poll;
automated startup/execution can use a bounded status check after a short wait.
Repeated unchanged status is not progress and does not justify repeated windows
or graph questions. An intentionally dismissed window reopens only after the
user requests continuation.

## Dialogue handoff

- `session_marker` is the returned `session_id`, which the user may give to another
  dialogue. Keep `conversation_id` local to its original dialogue.
- The receiving dialogue creates its own conversation marker once, then calls
  `attach_edit_session(session_id, conversation_id)` when the user requests takeover.
  The original approval window must be closed and its executor must be idle.
  A busy window or executor blocks handoff; close it before retrying.
- A stale `executing` record can be handed off for recovery when the actual window,
  executor locks and legacy approval process check confirm it has stopped. Handoff
  does not change that transaction's execution result.
- Successful handoff retains the same backup, plan and transaction, changes the
  owner, and invalidates the old dialogue's access and in-flight mutations. It
  does not approve or execute statements. Use the returned transaction ID with
  `reopen_approval` and the new marker to open the flow in the receiving dialogue.
- `attached=true` remains a successful transfer even when subsequent baseline
  diagnostics are unavailable. Inspect `baseline_status=current/stale/unknown`
  and its comparison time; stale evidence still permits handling/cancellation,
  but does not permit editing or execution from that old baseline.
- An old session handling window cannot cancel, recover, reopen or finish a flow
  after handoff. Status is scoped to the calling dialogue. Omitting a marker
  accesses only legacy sessions, never all dialogues.
