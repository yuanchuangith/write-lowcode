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
2. Call begin_edit_session for exactly one action code or RefId. Do not start another session until the first session is finished or safely aborted.
3. Confirm the returned draft is unpublished, active, backed up, fully covered by the compiler registry, and bound to the configured frontend commit/workspace, bridge hash, rule hash, and stable registry hash. Compare editor_status source paths with gxp-lowcode-readonly source_repository_status when both are available; resolve different repository selections before planning.
4. Express the requested change only as structured canvas operations. Never supply js_code, csharp_code, SQL, node keys for new nodes, or published design identifiers.
5. Call plan_canvas_patch. A plan exists only if the frontend compiler accepts the resulting canvas, a second validation returns the same result, and each action type has its required generated code. Report group, anchor, elementKey, generated nodes, canvas positions, parameters, compiler binding, generated-code hashes, relation changes, and canvas validation. If this call fails, state that no plan artifact, approval transaction, or database write was created.
6. Ask the user whether to open the local approval application. Call prepare_write only after explicit user authorization. It must recheck the current draft, frontend source/bridge/rule/registry binding, candidate JSON, validation result, code completeness, and all candidate hashes before creating any transaction or opening approval.
7. The local approval application, not the model, approves each SQL statement and executes the transaction. Poll only with get_write_status.
8. When the database transaction is saved, call verify_saved_draft. Do not claim success from the approval window alone.
9. Call finish_edit_session only after verification. State explicitly that the editor did not call a backend editor endpoint, clear Redis, write DLL/PDB, or publish. The database draft save happens before publication. Tell the user to inspect the ActionDesign canvas and publish manually so the normal backend path can generate the dynamic DLL; do not claim runtime completion before that manual step.

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
- Unknown elementKey, dynamic plugin without compiler adapter, invalid block nesting, duplicate key, invalid variable/action reference, or any frontend commit/workspace, bridge, rule, registry, or compiler-version drift.
- `validation.valid` is not exactly true, the candidate cannot be revalidated, compiler metadata is missing, `relation_data` is not an object, or generated-code hashes do not match the candidate.
- A JS action exists but generated `js_code` is blank, a C# action exists but generated `csharp_code` is blank, or a non-empty canvas generates no executable code.
- Concurrent modification, unexpected affected rows, expired/tampered approval, uncertain committed state, or any request to approve on the user's behalf.
- Any attempt to call the existing backend save/publish APIs, connect to Redis, or write/delete DLL/PDB as part of the editor workflow.

## Advisory runtime evidence

- Only local runtime-reference compatibility is advisory. Frontend source binding, canvas validation, and JS/C# generation completeness are mandatory prerequisites for saving `data`, `js_code`, `csharp_code`, or relation rows.
- `development_build`, missing references, compilation failure, and reference drift remain visible warnings. They do not bypass or weaken any database safety gate.
- A successful local check does not prove publication, dynamic DLL generation, cache invalidation, or runtime execution.

Read references/safety-contract.md for invariants and references/patch-schema.md before constructing operations. Read references/frontend-compiler-contract.md when checking compiler readiness, interpreting a blocked plan, or explaining why approval was not opened.
The approval review section in references/frontend-compiler-contract.md explains component/scope differences and display-only temporary-name hints; use it when interpreting the component and code comparison tabs.
