# Failed batches and performance

Read the repair section for rejected operations; read timings only when investigating latency. Neither branch changes the approval or verification gates.

## Repair a failed batch without starting over

`plan_canvas_patch` internally preflights the entire batch before native generation and C# compilation. There is no extra required tool call. It checks operation fields, exact target identities, JSON pointers, complete old-value guards, arrays and batch-local aliases against a private baseline copy. Sequential parameter updates expect the preceding operation's result. Later operations that need native generated keys/defaults are listed as `pending_checks`; they are not falsely reported as validated.

Read the bounded `error_details`: `phase`, `operation_index` (zero based), `group_key`, `node_key`, `path`, `reason_code`, `expected_shape` and `refresh_scope`. `issues` aggregates independently decidable errors; `issue_count` and `issues_truncated` reveal omitted diagnostics. Fix the shown errors and resubmit the whole corrected batch; another bounded response may reveal remaining issues. `pending_checks` requires native generation, not guessed keys. Do not infer an exact node from a group-only compiler diagnostic.

Each issue's `context_ref` provides a focused read using verified baseline identities. Supply this request's session/conversation when asked; group-only locations can use `find_edit_nodes` scoped to that group or available readonly `inspect_action`, but never invent a node from a group-only diagnostic. At most 20 issues are returned; after correcting them, resubmit the complete batch to obtain remaining errors. Errors omit parameter values; read complete parameters through the indicated context. The transport also supplies `failure_stage` and stable `error_code` for returned and thrown failures.

Keep the user's original `review_spec`, preservation rules and scenarios. Each planning attempt starts from the immutable session baseline, never from the failed candidate. A failed retry does not authorize submitting an older pending plan. Reuse valid diagnosis/location evidence; refresh only the indicated target or changed source binding. Do not switch full/incremental mode to evade a failure, loosen requirements or modify unrelated business callbacks.

Confirmed requirements are checkpointed separately from disposable candidate reports, bound to this session's baseline/compiler. Missing or corrupt candidate files can be regenerated without losing those requirements. Changed requirements survive a failed generation and block approval of the older plan; concurrent changes are checked atomically. If a legacy session has no checkpoint and its candidate is unreadable, follow `legacy_review_spec_unavailable` recovery rather than submitting an empty replacement spec. Same-dialogue exact-target/mode/queue begin retries reuse a verified session and return `next_tool`; they do not cancel it or inherit new approval.

## Timing and reusable preparation

MCP responses expose `trace_id`, `total_elapsed_ms`, `timings` with bounded stages, durations, counts/cache hits and stable error codes. Failures retain completed timings. Use them to distinguish database reads, evidence checks, preflight, registry/contract loading, generation, independent regeneration, runtime reference checks, SDK discovery, restore, build and business review. They measure tool work, not the model's reasoning or network round trip. Performance logs contain no parameter values, code, credentials or exception text; timings never enter signed plan content or business fingerprints.

Local spans can include `start_ms` and `depth`; nested durations overlap, so do not
sum them as independent costs. Readiness evidence is reused only for adjacent
consumers in one request. Prepare checks the compiler again after draft/relation
reads; independent Node and C# validation remain serial and retain their gates.
Node paging still validates the complete canvas before constructing the current
page, including duplicate keys and invalid nodes outside the displayed range.

Registry and component-contract caches are process-local and bounded. Keys include normalized source paths, content fingerprints, bridge/adapter/protocol and Node identity; Git provenance is refreshed. Generation and each independent verification still run in fresh Node processes. No candidate success result is cached, and content drift is checked even when length and modification time are unchanged.

C# workspaces reuse only validated restore preparation, bound to actual SDK identity, target framework, DLL content fingerprints and project/NuGet template versions. Exclusive cross-process slots isolate each candidate; source and build outputs are scrubbed between leases. Cold preparation restores packages; every nonblank candidate build uses `--no-restore --no-incremental`. Planning and approval each compile the complete candidate again, with reference fingerprints checked before and after. Dirty, interrupted or corrupt preparation is rebuilt cold; repeated failures return diagnostics rather than using old success. Busy or timed-out checkers remain blocking.

Pure Candidate.cs `CSxxxx` code errors may retain verified restore preparation after reference/assets checks and source/output cleanup, so corrected code need not restore again. Environment, mixed, metadata, timeout and drift failures remain cold. Adjacent registry/binding reads share only their immediate verified snapshot; every independent Node generation/verification still checks source, bridge and adapter before and after execution.

These changes do not weaken canvas/code checks, human SQL approval or saved-draft verification. Full generation remains the default; honor the user's generation mode choice. Tool responses default to summaries with explicit full reads. Source maintenance does not install/update the running plugin, restart Codex, save or publish a business draft. Assess speed from measured stages and cold/warm comparisons, never claim an unsupported percentage.
