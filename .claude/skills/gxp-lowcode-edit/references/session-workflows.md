# Session workflows

Read only the section for the current session state. These procedures retain the
compiler, backup, ownership, concurrency and human approval gates in SKILL.md.
A previously authorized same-dialogue, same-draft workflow may continue without
repeating explicit skill invocation; no section authorizes a new edit target.

## Blocked session entry

Without an explicitly requested queue, a dialogue owns at most one active
session. A different target in the same dialogue returns `blocked=true` and
opens a local session handling window before backup or compiler work. Another
dialogue's exclusive target reports the conflict without opening its window.
Closing the handling window retains its session; never treat it as cancellation.
`abort_edit_session` opens this window and returns `aborted=false`. After the
human resolves it, recheck `editor_status.active_session` before retrying entry.
Cancellation invalidates unexecuted plans and approvals while retaining backup
and audit records. For a chat-authorized cancellation, use the procedure below;
the handling window remains available. Never forcibly clear live or uncertain execution.

## Chat-authorized cancellation of unexecuted work

When the user wants AI to cancel an old unexecuted workflow, call
`inspect_session_cancellation(session_id, conversation_id)`. Show the action name
and code, the exact plan/approval IDs and the effect: invalidate that review,
release the local session, retain current business data, backups and history.
If `cancellable=false`, explain the returned reason and use recovery or the
handling window; do not fabricate a cancellation fingerprint.

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

## Explicit multi-action queues

For an explicitly authorized request covering several identified actions, call
`create_edit_queue(conversation_id)` once and pass `queue_id` to each session
entry. Keep expectations, backup, plan and transaction separate for each draft.
`prepare_write` opens the queue window for the first ready action; later ready
actions join that window automatically. Do not reopen each queued action.
Use `get_edit_queue` to track all items, including preparation still in progress.
The human approves single-row statements and confirms each action's save, with
no inherited approvals. These are independent transactions: a later failure
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
6. Open the validated repair approval directly with `prepare_write`, then retain per-statement human
   approval, saved-draft verification and finish steps. `prepare_write` rechecks
   reference provenance and the complete candidate; the approval window includes
   a signed, read-only “引用修复来源” tab. The bindings explicitly selected for repair
   must resolve; other static variable/action diagnostics are advisory.
   Publication remains manual.

## Restore workflow

- Call prepare_restore with the session backup identifier.
- Restoration is a new approval transaction; never describe it as automatic rollback.
- Poll get_write_status, then call verify_saved_draft.
- Finish only when the restored hashes match the immutable backup.

## Session recovery

- New approval saves verify the committed draft and relations and release the
  session in the save worker before showing a publication prompt. Completed
  sessions retain a `saved_verification` receipt. Status/verification calls reuse
  that historical result without comparing it to code regenerated by a later
  platform save or publication. This does not verify those later changes.
- A `saved` transaction without `verified=true` and `session_released=true` still
  needs verification and finish, including saves made by older approval apps.
  Do not prompt platform saving/publication while that session is unfinished.
- If the approval window fails to launch, retry once with `reopen_approval` and the returned transaction ID without another opening confirmation; report a repeated launch failure instead of looping.
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
- The executor and recovery share an OS lock. Recovery cannot release a live
  executor. Old executing records without this protocol cannot be assumed dead.
- If neither snapshot matches, the session window offers an explicit human choice
  to keep current database data and close the session. This archives the mismatch,
  does not restore or overwrite data, and does not assert successful execution.
- If configuration, database access, a missing/corrupt approval file, a damaged
  local baseline, or a legacy execution record prevents verification, open
  `abort_edit_session` with this dialogue's marker. Its separate “保留数据并释放本地流程”
  button lets the user explicitly archive an unverified flow and retain current
  database data without depending on configuration, compilers or database reads.
  The user must close all approval programs first. Window/executor locks and a
  legacy process check block release while the workflow is still running. Closure
  retains backups and an audit snapshot, invalidates remaining plans/approvals,
  and never reports successful verification. Never call the local closure helper
  from the model or treat opening this window as completed release.
- When entry is blocked, the session handling window is also available for saved,
  verified, pending restore, and uncertain sessions; do not repeatedly retry entry.

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
- An old session handling window cannot cancel, recover, reopen or finish a flow
  after handoff. Status is scoped to the calling dialogue. Omitting a marker
  accesses only legacy sessions, never all dialogues.
