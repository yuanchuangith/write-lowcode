# Reuse diagnosis evidence and repair failed operations

## Local readonly handoff

This is design-evidence reuse, not the live-session transfer described in `session-workflows.md`. The explicit invocation gate still applies: a readonly export, confirmed proposal or handoff record does not activate the editor. Once activated and the current scope is authorized, reuse that confirmation within the same dialogue; do not repeatedly ask.

The readonly plugin exports `export_edit_handoff(identifier, targets, change_spec, diagnosis_source)`. It captures the current draft separately from the exact current published/draft copy used for diagnosis. Pass its `handoff_ref` unchanged:

```json
{
  "identifier": "<verified action code or RefId>",
  "conversation_id": "<this dialogue's registered conversation ID>",
  "handoff_ref": {
    "handoff_id": "<32 lowercase hexadecimal characters>",
    "sha256": "<64 lowercase hexadecimal characters>"
  }
}
```

`begin_edit_session` checks environment destination, action/draft identity, content hash, exact target parameters, parents, neighbours and complete group context before creating occupancy. A matched result includes `handoff` and target `edit_context`; reuse them rather than immediately requesting the same context again. Group-context hashes guard content, including flat IF conditions and group input/output configuration; they do not prove semantic control-flow validation. An unrelated change in the same group may conservatively require refreshed evidence.

`handoff_status="stale"` reports changed draft targets. `source_target_diverged` reports differing fields between the diagnosed copy and the editable draft. Inspect only those locations and dependencies, revise the proposal if necessary, then export a refreshed handoff or enter through ordinary targeted verification. Do not overwrite the draft with published parameters. Missing, corrupt or unsupported records return the ordinary inspection path without a new session; no plugin reinstall or restart is necessary.

In a new dialogue, use readonly `read_edit_handoff(identifier=...)` to list candidate summaries, explicitly select `handoff_id` and `sha256`, and confirm the current change scope. Never automatically select the newest record or reuse the former dialogue's conversation ID, session, plan or approval. Check `response_complete` and follow `next_read` / `target_index` for oversized records; omitted parameters are not empty parameters. Oversized metadata may return `HANDOFF_RESPONSE_LIMIT`; the complete local record remains available for targeted inspection.

Records are stored under `%LOCALAPPDATA%\GxpLowcodeHandoff\v1` on the same machine. They contain bounded design evidence and the original request, proposed changes, preservation requirements and scenarios. No credentials, business query rows or complete generated JS/C# are transferred. Accept IDs, never arbitrary paths. A checksum is an integrity check, not authorization or human approval.

## Templates and complete parameters

For verified targets call `inspect_edit_context(session_id, targets, operation_cursor=0, conversation_id)`. `nodes[].params_value` remains the complete native parameter object; the tool also returns exact native contracts, visible variables, location/parents, and `edit_options`:

- `parameter_operations` contains incomplete safe operation templates with exact paths. Fill `required_inputs` from the referenced complete old value and intended replacement; never submit placeholders or treat `complete=false` as a runnable operation.
- `atomic_parameter_paths` identifies ParamInput records that must be changed as complete records. Preserve native keys and synchronize the applicable value/code members.
- Array rows use a unique existing native `id`/`key` selector when advertised. Numeric array indexes remain unsupported; otherwise replace the whole array while preserving unchanged rows.
- `insert_in_block` is a capability hint; native block validation still decides whether insertion is valid.

Templates are paged in groups of at most 64. Continue with `next_operation_cursor`, identical ordered targets and the same `context_binding`. The first page contains full context; later pages default to `context_detail="compact_guards"`, explicitly omitting repeated parameters/parents/contracts. Resolve each template's `expected_old_value_ref.guard_index` into `nodes[].parameter_guards[index].value` and copy that exact JSON value into `expected_old_value`; the reference object itself is not a valid old value. Pass `include_context=true` for full context on any page or `false` to omit unrelated context. No parameter or guard is truncated. Refresh if the binding changes. The response is bounded to 1 MiB; follow the explicit size-error path or request fewer nodes. Native editor/generator function bodies are not parsed into the registry; inspect source only when structured evidence is insufficient. Neither context bindings nor guards grant approval.

## Repair a failed batch without starting over

`plan_canvas_patch` internally preflights the entire batch before native generation and C# compilation. There is no extra required tool call. It checks operation fields, exact target identities, JSON pointers, complete old-value guards, arrays and batch-local aliases against a private baseline copy. Sequential parameter updates expect the preceding operation's result. Later operations that need native generated keys/defaults are listed as `pending_checks`; they are not falsely reported as validated.

Read the bounded `error_details`: `phase`, `operation_index` (zero based), `group_key`, `node_key`, `path`, `reason_code`, `expected_shape` and `refresh_scope`. `issues` aggregates independently decidable errors; `issue_count` and `issues_truncated` reveal omitted diagnostics. Fix the shown errors and resubmit the whole corrected batch; another bounded response may reveal remaining issues. `pending_checks` requires native generation, not guessed keys. Do not infer an exact node from a group-only compiler diagnostic.

Each issue's `context_ref` provides a focused read using verified baseline identities. Supply this request's session/conversation when asked; group-only locations use readonly `inspect_action` and never invent a node. At most 20 issues are returned; after correcting them, resubmit the complete batch to obtain remaining errors. Errors omit parameter values; read complete parameters through the indicated context. The transport also supplies `failure_stage` and stable `error_code` for returned and thrown failures.

Keep the user's original `review_spec`, preservation rules and scenarios. Each planning attempt starts from the immutable session baseline, never from the failed candidate. A failed retry does not authorize submitting an older pending plan. Reuse valid diagnosis/location evidence; refresh only the indicated target or changed source binding. Do not switch full/incremental mode to evade a failure, loosen requirements or modify unrelated business callbacks.

Confirmed requirements are checkpointed separately from disposable candidate reports, bound to this session's baseline/compiler. Missing or corrupt candidate files can be regenerated without losing those requirements. Changed requirements survive a failed generation and block approval of the older plan; concurrent changes are checked atomically. If a legacy session has no checkpoint and its candidate is unreadable, follow `legacy_review_spec_unavailable` recovery rather than submitting an empty replacement spec. Same-dialogue exact-target/mode/queue begin retries reuse a verified session and return `next_tool`; they do not cancel it or inherit new approval.

## Timing and reusable preparation

MCP responses expose `trace_id`, `total_elapsed_ms`, `timings` with bounded stages, durations, counts/cache hits and stable error codes. Failures retain completed timings. Use them to distinguish database reads, evidence checks, preflight, registry/contract loading, generation, independent regeneration, runtime reference checks, SDK discovery, restore, build and business review. They measure tool work, not the model's reasoning or network round trip. Performance logs contain no parameter values, code, credentials or exception text; timings never enter signed plan content or business fingerprints.

Registry and component-contract caches are process-local and bounded. Keys include normalized source paths, content fingerprints, bridge/adapter/protocol and Node identity; Git provenance is refreshed. Generation and each independent verification still run in fresh Node processes. No candidate success result is cached, and content drift is checked even when length and modification time are unchanged.

C# workspaces reuse only validated restore preparation, bound to actual SDK identity, target framework, DLL content fingerprints and project/NuGet template versions. Exclusive cross-process slots isolate each candidate; source and build outputs are scrubbed between leases. Cold preparation restores packages; every nonblank candidate build uses `--no-restore --no-incremental`. Planning and approval each compile the complete candidate again, with reference fingerprints checked before and after. Dirty, interrupted or corrupt preparation is rebuilt cold; repeated failures return diagnostics rather than using old success. Busy or timed-out checkers remain blocking.

Pure Candidate.cs `CSxxxx` code errors may retain verified restore preparation after reference/assets checks and source/output cleanup, so corrected code need not restore again. Environment, mixed, metadata, timeout and drift failures remain cold. Adjacent registry/binding reads share only their immediate verified snapshot; every independent Node generation/verification still checks source, bridge and adapter before and after execution.

These changes do not weaken canvas/code checks, human SQL approval or saved-draft verification. Full remains the default; honor the user's mode choice. Source maintenance does not install/update the running plugin, restart Codex, save or publish a business draft. Assess speed from measured stages and cold/warm comparisons, never claim an unsupported percentage.
