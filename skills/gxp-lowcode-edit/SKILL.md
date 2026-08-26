---
name: gxp-lowcode-edit
description: Guarded editing of one existing unpublished GXP ActionDesign draft. Use when the user asks to modify a GXP public or form action canvas, add or change real ActionDesign nodes/components, prepare an approval-backed draft save, verify the saved draft, or restore the pre-edit snapshot through the gxp-lowcode-editor MCP. Do not use for creating designs, publishing, editing published rows, arbitrary SQL, or bypassing human approval.
---

# GXP Low-Code Edit

Use the gxp-lowcode-editor MCP in the exact order below. Treat every stop condition as mandatory.

## Fixed workflow

1. Call editor_status. `planning_ready` permits backup and planning; `write_ready` is required before opening approval. Local runtime/DLL validation is advisory and is reported separately through `runtime_compatibility_verified`; it never changes `planning_ready` or `write_ready`. Prefer a dedicated least-privilege account. A shared application account may use `account_mode=shared_restricted` when it has no global/server-level privileges. A user may explicitly opt into `account_mode=shared_unrestricted` for a privileged shared account only through the local configuration confirmation. Both shared modes require a pinned, matching `SHOW GRANTS` fingerprint and a ready account-safety gate.
2. Call begin_edit_session for exactly one action code or RefId. Do not start another session until the first session is finished or safely aborted.
3. Confirm the returned draft is unpublished, active, backed up, and fully covered by the compiler registry.
4. Express the requested change only as structured canvas operations. Never supply js_code, csharp_code, SQL, node keys for new nodes, or published design identifiers.
5. Call plan_canvas_patch. Report group, anchor, elementKey, generated nodes, canvas positions, parameters, generated-code hashes, relation changes, canvas validation, and any local C# compilation evidence as advisory only. A missing, development-build, failed, or changed local runtime reference must not be described as a database-write failure or as runtime success.
6. Ask the user whether to open the local approval application. Call prepare_write only after the user explicitly requests the save workflow.
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
- Unknown elementKey, dynamic plugin without compiler adapter, invalid block nesting, duplicate key, invalid variable/action reference, or compiler version drift.
- Concurrent modification, unexpected affected rows, expired/tampered approval, uncertain committed state, or any request to approve on the user's behalf.
- Any attempt to call the existing backend save/publish APIs, connect to Redis, or write/delete DLL/PDB as part of the editor workflow.

## Advisory runtime evidence

- Local C# compilation and runtime-reference inspection may identify compatibility risks, but they are not prerequisites for saving `data`, `js_code`, `csharp_code`, or relation rows to the unpublished draft.
- `development_build`, missing references, compilation failure, and reference drift remain visible warnings. They do not bypass or weaken any database safety gate.
- A successful local check does not prove publication, dynamic DLL generation, cache invalidation, or runtime execution.

Read references/safety-contract.md for invariants and references/patch-schema.md before constructing operations.
