---
name: gxp-lowcode-edit
description: Explicit-invocation-only, compiler-backed editing of one existing unpublished GXP ActionDesign draft. Use only when the current user message explicitly invokes gxp-lowcode-edit to implement a canvas plan, modify real nodes/components, prepare an approval-backed draft save, verify the saved draft, or restore its pre-edit snapshot. The workflow binds the real gxp2.components compiler source and blocks invalid canvases or incomplete generated JS/C#. Do not infer activation from a readonly diagnosis, a proposed plan, “continue”, or “apply the plan”. Do not use for creating or publishing designs, editing published rows, arbitrary SQL, or bypassing human approval.
---

# GXP Low-Code Edit

Use the gxp-lowcode-editor MCP in the exact order below. Treat every stop condition as mandatory.

## Explicit invocation gate

- Enter this skill only when the current user message explicitly invokes `$gxp-lowcode-edit` or its plugin-qualified form. `agents/openai.yaml` must keep `policy.allow_implicit_invocation: false`.
- A plan produced by `gxp-lowcode-debug`, a readonly skill suggesting this editor, or a later message that only says “继续”, “按方案改”, or “实施前面的方案” is not an invocation. Do not call `editor_status` or any editor MCP tool; ask the user to invoke `$gxp-lowcode-edit` explicitly and stop.
- After explicit invocation, the readonly plan is only an input candidate. Re-read the current draft and independently pass every compiler, safety, backup, concurrency, and approval gate before using it.
- Explicit skill invocation authorizes entry into guarded planning only. It does not authorize `prepare_write`, statement approval, publication, or any other mutation; retain the separate confirmations below.

## Fixed workflow

Before constructing any operations, read [references/targeting-and-source.md](references/targeting-and-source.md). Locate the target using current ActionDesign evidence and verify its parameter contract against the real element registry/source when needed. Repository paths support source inspection; they do not identify which business node is faulty.

1. Call editor_status. `planning_ready` requires a verified compiler bridge and frontend Git provenance; `write_ready` is required before opening approval. Treat the compiler gate as blocking. Local runtime/DLL compatibility remains advisory and is reported separately through `runtime_compatibility_verified`. Prefer a dedicated least-privilege account. A shared application account may use `account_mode=shared_restricted` when it has no global/server-level privileges. A user may explicitly opt into `account_mode=shared_unrestricted` for a privileged shared account only through the local configuration confirmation. Both shared modes require a pinned, matching `SHOW GRANTS` fingerprint and a ready account-safety gate.
2. Call begin_edit_session for exactly one action code or RefId. Do not start another session until the first session is finished or safely aborted. If an old unexecuted session exists, the tool returns `blocked=true` and opens a local keep/abort window before any backup or compiler work. The user must manually choose whether to retain or abort it; closing the window retains it. `abort_edit_session` only opens that window and returns `aborted=false`, not a completed abort. Recheck `editor_status.active_session` before retrying. A pending approval can be cancelled only if execution has never started; the cancellation also invalidates its ready plans and pending transactions atomically while retaining backups and audit records. Executing, failed/uncertain, saved and restore sessions must not be forcibly cleared; verified sessions use finish_edit_session.
3. Confirm the returned draft is unpublished, active, backed up, fully covered by the compiler registry, and bound to the configured frontend commit/workspace, bridge hash, rule hash, and stable registry hash. Compare editor_status source paths with gxp-lowcode-readonly source_repository_status when both are available; resolve different repository selections before planning.
4. Express the requested change only as structured canvas operations. Never supply js_code, csharp_code, SQL, node keys for new nodes, or published design identifiers.
5. Call plan_canvas_patch. A plan exists only if the frontend compiler accepts the resulting canvas, a second validation returns the same result, and each action type has its required generated code. Report group, anchor, elementKey, generated nodes, canvas positions, parameters, compiler binding, generated-code hashes, relation changes, and canvas validation. If this call fails, state that no plan artifact, approval transaction, or database write was created.
6. Ask the user whether to open the local approval application. Call prepare_write only after explicit user authorization. It must recheck the current draft, frontend source/bridge/rule/registry binding, candidate JSON, validation result, code completeness, and all candidate hashes before creating any transaction or opening approval.
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
   session lock as usual. No second session may start while this one is active.
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
6. Continue with the normal `prepare_write` authorization, per-statement human
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

## Advisory runtime evidence

- Static variable/action reference diagnostics and local runtime-reference compatibility are advisory. Frontend source binding, structural canvas validation, and JS/C# generation completeness are mandatory prerequisites for saving `data`, `js_code`, `csharp_code`, or relation rows. Report `validation.warnings` without requiring unrelated reference repairs or confirmation solely because of those warnings.
- `development_build`, missing references, compilation failure, and reference drift remain visible warnings. They do not bypass or weaken any database safety gate.
- A successful local check does not prove publication, dynamic DLL generation, cache invalidation, or runtime execution.

Read references/safety-contract.md for invariants and references/patch-schema.md before constructing operations. Read references/frontend-compiler-contract.md when checking compiler readiness, interpreting a blocked plan, or explaining why approval was not opened.
The approval review section in references/frontend-compiler-contract.md explains component/scope differences and display-only temporary-name hints; use it when interpreting the component and code comparison tabs.
