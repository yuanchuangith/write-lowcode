---
name: gxp-lowcode-edit
description: Explicit-invocation-only, compiler-backed editing of one existing unpublished GXP ActionDesign draft. Use only when the current user message explicitly invokes gxp-lowcode-edit to implement a canvas plan, modify real nodes/components, add child action groups or bind component events within that draft, prepare an approval-backed draft save, verify the saved draft, or restore its pre-edit snapshot. The workflow binds the real gxp2.components compiler source and blocks invalid canvases or incomplete generated JS/C#. Do not infer activation from a readonly diagnosis, a proposed plan, “continue”, or “apply the plan”. Do not use for creating or publishing designs, editing published rows, arbitrary SQL, or bypassing human approval.
---

# GXP Low-Code Edit

Use the gxp-lowcode-editor MCP in the exact order below. Treat every stop condition as mandatory.

## Explicit invocation gate

- Enter this skill only when the current user message explicitly invokes `$gxp-lowcode-edit` or its plugin-qualified form. `agents/openai.yaml` must keep `policy.allow_implicit_invocation: false`.
- A plan produced by `gxp-lowcode-debug`, a readonly skill suggesting this editor, or a later message that only says “继续”, “按方案改”, or “实施前面的方案” is not an invocation. Do not call `editor_status` or any editor MCP tool; ask the user to invoke `$gxp-lowcode-edit` explicitly and stop.
- After explicit invocation, the readonly plan is only an input candidate. Re-read the current draft and independently pass every compiler, safety, backup, concurrency, and approval gate before using it.
- Explicit skill invocation authorizes guarded planning and opening the local approval application for a validated plan. Once approval content is ready, open it directly without asking for a separate chat confirmation. Opening or reopening the window never approves SQL, executes a transaction, or publishes a design; retain per-statement human approval and final execution confirmation inside the application.

## Fixed workflow

Before constructing any operations, read [references/targeting-and-source.md](references/targeting-and-source.md). Locate the target using current ActionDesign evidence and verify its parameter contract against the real element registry/source when needed. Repository paths support source inspection; they do not identify which business node is faulty.

1. On first use in this dialogue, call `create_conversation(label)` and retain its returned `conversation_id`. Reuse that marker for this dialogue; pass it on every subsequent editor tool call, including `editor_status`, planning, approval status, recovery and finish. Never reuse another dialogue's marker. `planning_ready` requires a verified compiler bridge and frontend Git provenance; `write_ready` is required before opening approval. Treat the compiler gate as blocking. Nonblank candidate C# must compile locally before planning or approval; checker unavailability, timeout or compilation failure blocks that candidate. Development-reference success does not establish production compatibility. `production_ready` stays false until separate deployment/runtime acceptance evidence exists. Prefer a dedicated least-privilege account. A shared application account may use `account_mode=shared_restricted` when it has no global/server-level privileges. A user may explicitly opt into `account_mode=shared_unrestricted` for a privileged shared account only through the local configuration confirmation. Both shared modes require a pinned, matching `SHOW GRANTS` fingerprint and a ready account-safety gate.
2. Call begin_edit_session for exactly one action code or RefId. This dialogue may own one active session; other dialogues may independently edit different drafts and open their own approval windows. The same design ID or RefId remains exclusive. If this dialogue already has a session, the tool returns `blocked=true` and opens a local session handling window before any backup or compiler work. If another dialogue owns the target, entry reports the conflict without opening its handling window. To take over an existing flow, follow Dialogue handoff below. The user may retain a local session, cancel unexecuted approval, reopen approval, or reconcile completed/interrupted execution. Closing the window retains it. `abort_edit_session` opens this window and returns `aborted=false`, not a completed abort. Recheck `editor_status.active_session` before retrying. Cancellation invalidates its ready plans and pending transactions atomically while retaining backups and audit records. Confirmed no-write failures and rollbacks release ordinary edit sessions automatically; restore failures return to the preceding save. The window may also recover legacy pre-write failures recognized by their exact concurrency/grant rejection messages. Do not forcibly clear live execution or uncertain data; follow Session recovery below. Verified sessions use finish_edit_session.
3. Confirm the returned draft is unpublished, active, backed up, fully covered by the compiler registry, and bound to the configured frontend commit/workspace, bridge hash, rule hash, and stable registry hash. Compare editor_status source paths with gxp-lowcode-readonly source_repository_status when both are available; resolve different repository selections before planning.
4. Check `editor_status.compiler.supported_operations`. For new child groups, empty-group inserts or component event binding, read [references/patch-schema.md](references/patch-schema.md). Query `inspect_component_contract` for native event signatures and use readonly page evidence to verify the exact component instance. Creating a child group stays within the existing draft; page-designer event properties are outside this workflow. Express the requested change only as structured canvas operations. Never supply js_code, csharp_code, SQL, node keys for new nodes, or published design identifiers.
For every new or changed event callback, first read the event-contract section in [references/frontend-compiler-contract.md](references/frontend-compiler-contract.md). Verify actual input payloads against runtime source when catalog entries are incomplete. Declare visible callback inputs; never work around missing parameters with group-level arguments or undeclared identifiers. Build a small scenario matrix from the current request: each button/branch, unmatched input, empty selection/value, and sequential events when registration or retained state matters. Describe expected metadata keys, values and side effects, not only whether the callback runs. Prefer native IF nodes for branch-specific side effects; ternaries assigning empty values do not skip event registration. Unknown expectations stay unresolved and must not be reported as passing.

5. Call plan_canvas_patch. A plan exists only if the frontend compiler accepts the resulting canvas, a second validation returns the same result, and each action type has its required generated code. Report new group aliases and generated group/parameter keys, component/event/callback bindings, group, anchor, elementKey, generated nodes, canvas positions, parameters, compiler binding, generated-code hashes, relation changes, and canvas validation. If this call fails, state that no plan artifact, approval transaction, or database write was created.
6. Once the requested plan is validated and `write_ready` is true, call `prepare_write` immediately to open the local Qt approval application. Do not ask the user to type “open”, “打开”, or give another chat confirmation. Briefly report that the review window is opening. The tool must recheck the current draft, frontend source/bridge/rule/registry binding, candidate JSON, validation result, code completeness, and all candidate hashes before creating any transaction or opening approval. Leave statement approval and final execution to the human inside the application.
7. The local approval application, not the model, approves each SQL statement and executes the transaction. Poll only with get_write_status.
8. When the database transaction is saved, call verify_saved_draft. Do not claim success from the approval window alone.
9. Call finish_edit_session only after verification. State explicitly that the editor did not call a backend editor endpoint, clear Redis, write DLL/PDB, or publish. The database draft save happens before publication. Tell the user to inspect the ActionDesign canvas and publish manually so the normal backend path can generate the dynamic DLL; do not claim runtime completion before that manual step.

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
   draft ownership as usual. This dialogue cannot own a second active session;
   other dialogues may work on other drafts.
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
   native nodes and all JS/C#/relations, and revalidates the whole result. Failure
   creates no plan, approval transaction or database write. Ordinary
   `plan_canvas_patch` cannot operate on a repair session.
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

## Hard stop conditions

- Missing required indexes, an unpinned or changed shared-account grant fingerprint, or unsafe dedicated-account grants. Global non-USAGE and server-administration privileges remain a hard stop in `shared_restricted`; they are permitted only in explicitly confirmed `shared_unrestricted`, remain a high risk outside this MCP, and are rebound by fingerprint before every write.
- More than one active draft for the resolved RefId.
- Published, deleted, or newly created design.
- Missing backup or invalid backup manifest.
- Unknown elementKey, dynamic plugin without compiler adapter, invalid block nesting, duplicate key, or any frontend commit/workspace, bridge, rule, registry, or compiler-version drift. Static variable/action reference diagnostics are warnings, not hard stops. Existing references need not be repaired before an ordinary edit when structural validation and native code generation succeed. Do not claim runtime failure from a static reference warning alone.
- `validation.valid` is not exactly true, the candidate cannot be revalidated, compiler metadata is missing, `relation_data` is not an object, or generated-code hashes do not match the candidate.
- A JS action exists but generated `js_code` is blank, a C# action exists but generated `csharp_code` is blank, or a non-empty canvas generates no executable code.
- Concurrent modification, unexpected affected rows, expired/tampered approval, uncertain committed state, or any request to approve on the user's behalf.
- Any attempt to call the existing backend save/publish APIs, connect to Redis, or write/delete DLL/PDB as part of the editor workflow.

## Local compilation and runtime evidence

- Native structured-reference diagnostics remain advisory, while lexical checks now cover all JS groups and reject undeclared identifiers. Nonblank candidate C# compilation is mandatory. Frontend source binding, structural canvas validation, exact regeneration consistency, and JS/C# generation completeness are mandatory prerequisites for saving `data`, `js_code`, `csharp_code`, or relation rows. Report `validation.warnings` without requiring unrelated reference repairs or confirmation solely because of those warnings.
- A successful `development_build` check establishes only local-reference compilation. Missing references, compilation failure, timeout or reference drift blocks nonblank C# candidates; do not downgrade these failures to warnings or open approval.
- A successful local check does not prove publication, dynamic DLL generation, cache invalidation, or runtime execution.

Read references/safety-contract.md for invariants and references/patch-schema.md before constructing operations. Read references/frontend-compiler-contract.md when checking compiler readiness, interpreting a blocked plan, or explaining why approval was not opened.
The approval review section in references/frontend-compiler-contract.md explains component/scope differences and display-only temporary-name hints; use it when interpreting the component and code comparison tabs.
