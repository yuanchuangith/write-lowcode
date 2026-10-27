# Reuse diagnosis evidence

Read this reference when reusing dialogue findings or optional readonly records. For current target/value reads, use [targeting-and-source.md](targeting-and-source.md); live-session ownership transfer is in [session-workflows.md](session-workflows.md#dialogue-handoff).

## Check available evidence first

For missing evidence, tool errors or bounded automatic alternate reads, use [diagnostic-fallback.md](diagnostic-fallback.md). It defines the shared 3-call/30-second fallback budget and stop conditions without changing edit or approval gates.

Write works independently. Look may leave evidence and data for later reuse, but Look use, a formal handoff, `handoff_ref` and a complete diagnosis or fix are optional. First check the current dialogue and available records against the requested problem: are the findings relevant, sufficient for the next decision and applicable to the current draft? Reuse adequate material and avoid repeating searches that would add no evidence.

When material is missing, stale or contradictory, investigate the affected question directly or use available Look readonly tools. The user does not need to start another Look task or arrange a complete handoff. Keep supported findings, but revise an earlier cause, excluded explanation or proposed fix when new evidence warrants it; explain the basis and preserve the user's confirmed scope and business requirements. Evidence reuse must not block necessary investigation. Tool checks still establish current draft identity and target parameters before edits.

This is design-evidence reuse, not the live-session transfer described in `session-workflows.md`. The explicit invocation gate still applies: a readonly export, confirmed proposal or stored record does not activate the editor. Once activated and the current scope is authorized, reuse that confirmation within the same dialogue; do not repeatedly ask.

## Optional local readonly record

The readonly plugin can retain structured evidence with `export_edit_handoff(identifier, targets, change_spec, diagnosis_source)`. An `edit_snapshot` record captures the current draft separately from the exact current published/draft copy used for diagnosis. A `diagnosis_only` record retains evidence and unresolved questions before current targets or a draft are available; it has no draft or target snapshot. When using a verified record, pass its `handoff_ref` unchanged; otherwise omit it and use the ordinary inspection path:

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

Without a reference, normal `begin_edit_session` and same-session retries return optional `evidence_candidates`: at most five bounded local summaries for the exact action RefId and configured environment. Summaries show environment, draft/published diagnosis source, record kind, issue status and verification where known. They are discovery hints, not verified snapshots or permission to apply a fix. Read a relevant record explicitly with readonly `read_edit_handoff(handoff_id, sha256)` when useful; supplying its reference on a same-session retry verifies it without creating another session. A failed local lookup leaves ordinary editing available. Candidate pages are deliberately bounded and may be empty with `has_more=true` when unrelated files filled the scan budget. Use `next_read` / `next_cursor` only when more evidence is needed; do not automatically exhaust the store or choose the newest record.

For an `edit_snapshot` reference, `begin_edit_session` checks environment destination, action/draft identity, content hash, exact target parameters, parents, neighbours and complete group context before creating occupancy. New records also contain a semantic JSON hash: a raw formatting or object-key-order change can remain `matched` only when semantic content and every target snapshot are unchanged, explicitly reported by `freshness.formatting_only`. Array order remains significant. Older records without this hash retain conservative raw-content checks. A matched result includes `handoff` and target `edit_context`; reuse them rather than immediately requesting the same context again. Group-context hashes guard content, including flat IF conditions and group input/output configuration; they do not prove semantic control-flow validation. An unrelated change in the same group may conservatively require refreshed evidence.

For `diagnosis_only`, environment and action identity are checked and normal session entry may continue, but `snapshot_reused=false` and `requires_current_target_inspection=true`. Reuse applicable historical symptoms, findings and unresolved questions; locate and inspect current draft targets through the ordinary path. No `edit_context` is inferred from this record and no new export is required merely to continue.

### Reuse findings and fill the actual gaps

When a record is used, read its complete `change_spec` before constructing operations. Reuse its available problem statement, evidence, exact location, preservation requirements and scenarios, and assess any root cause or proposed solution against the current task. A matched `edit_context` already supplies current target parameters and contracts, so do not immediately request the same context again. Read further when a decision needs missing templates, dependencies or evidence, or when content or binding has changed. A record can be useful without a confirmed cause or a repair plan.

`change_spec.diagnosis` is optional for compatibility. When present it contains:

- `issues`: entries require `issue_id`, `status` (`confirmed` or `candidate`), `symptom`, `evidence` and `verification`. `expected`, `root_cause` and `solution` are optional; unknown values are omitted rather than invented. `confirmed` applies only to what the evidence establishes and does not supply a missing cause or solution.
- Each evidence entry contains `source` and `finding`, with optional `group_key`, `node_key` and `design_id` to locate the finding. These are bounded evidence descriptions, not copied business query rows or generated code.
- `verification` is `static_confirmed`, `runtime_verified` or `pending`. Optional `excluded` and `unresolved` arrays preserve excluded causes and remaining questions.

Keep `candidate` causes provisional; obtain the evidence needed for the relevant decision before treating a cause as confirmed. `confirmed` is not approval to implement a fix. `static_confirmed` does not prove runtime behavior. Even `runtime_verified` concerns the diagnosed behavior and its recorded source; it does not establish that a later patch was saved, published or accepted at runtime. Previously excluded causes help avoid repeated work, but can be reconsidered when evidence or current scope warrants it. Report unresolved limits and obtain any missing critical business rule.

For older records without `diagnosis`, reuse applicable locations, parameters and `change_spec` details. Check the existing dialogue and perform necessary focused analysis or reads. Absence of the field alone is not a reason to reject useful evidence or investigate from scratch.

### Develop the patch and preserve acceptance rules

- `changes` may be omitted or empty when the record only retains evidence. Develop the missing fix from supported findings and any necessary focused investigation. For an applicable proposal, translate each `changes` item into supported operations using current templates. Match its exact group/node, parameter path and complete `before` value against the bound context. If the proposed `after` value no longer fits the evidence or requirement, revise it with a stated basis. A proposal is not an executable operation, template placeholders are not values, and reuse does not authorize unrelated cleanup. Obtain relevant contract/template evidence for missing native wrappers, structural operations or declarations.
- Preserve the original `user_request` and previously confirmed rules in `review_spec.requirements[].source_text`; keep each requirement tied to exact verified targets. Carry `preserve` items into requirements for unchanged behavior and relevant `canvas_assertions`. A diagnosis or proposed solution does not supply new user authorization or justify a wider scope.
- Convert `scenarios` into distinguishing `review_spec.scenarios.cases` using the actual group/event contract and fixed input/expected values. Preserve their meaning and requirement links. When a scenario is only prose, fill its missing runner details from current evidence; keep unsupported runtime checks explicitly unverified rather than inventing fixtures, expected values or a passing result. Follow [business-review.md](business-review.md) for the native review schema.
- Establish these expectations before generation from the original request and confirmed business rules. Never derive an expectation from the repaired canvas, generated code or observed candidate result, and never weaken an expectation to make a repair pass.

In the same dialogue, a user can explicitly invoke `$gxp-lowcode-edit` and ask to fix the problem using existing material. If a suitable export exists, use its actual `handoff_ref` unchanged; do not ask the user to transcribe IDs already present in the dialogue. An export is not required to continue. This invocation still leads through normal compilation, formal human approval and saved-draft verification.

`handoff_status="stale"` does not always mean the target parameter changed. Read `stale_reason`, `target_differences`, `freshness` and `refresh_scope`:

- `target_content_changed`: inspect changed target parameters or identity, then the findings and dependencies they affect.
- `target_context_changed`: inspect changed parents, neighbours, group structure or group configuration. Unchanged target parameters alone do not establish unchanged behavior.
- `other_draft_content_changed`: target snapshots match, but semantic draft content elsewhere changed. Assess relevant groups, globals and dependencies before reusing conclusions; do not claim the target itself changed or automatically reread all its parameters.
- `legacy_content_changed`: the older record has no semantic hash, so a raw-content difference remains unverified even when target snapshots match.

These distinctions preserve stale-record safeguards. `source_target_diverged` reports differing fields between the diagnosed copy and the editable draft. Start with the returned scope, extend investigation if the evidence requires it, and revise the diagnosis or proposal as needed. A refreshed export is optional; ordinary targeted verification can continue directly without a reference. Do not overwrite the draft with published parameters. Missing, corrupt or unsupported records return the ordinary inspection path without a new session; no plugin reinstall or restart is necessary.

When looking for stored evidence in a new dialogue, first assess entry's optional candidates or use readonly `read_edit_handoff(identifier=..., environment_scope=..., cursor=..., limit=...)` for paged candidate summaries. Use the current environment scope provided by entry, select an applicable `handoff_id` and `sha256` explicitly, and confirm the current change scope. Do not reuse the former dialogue's conversation ID, session, plan or approval. This lookup is optional when current material already suffices or independent inspection is more appropriate. Check `response_complete` and follow `next_read` / `target_index` for oversized full records; candidate-list cursors and target-page indexes serve different reads. Omitted parameters are not empty parameters. Oversized metadata may return `HANDOFF_RESPONSE_LIMIT`; the complete local record remains available for targeted inspection.

Records are stored under `%LOCALAPPDATA%\GxpLowcodeHandoff\v1` on the same machine. They contain bounded design evidence and the original request, plus proposed changes, preservation requirements and scenarios when known. No credentials, business query rows or complete generated JS/C# are transferred. Accept IDs, never arbitrary paths. A checksum is an integrity check, not authorization or human approval.

## Related procedures

Each procedure has one maintained source. These section links preserve older callers without loading unrelated workflows.

## Review Look's graph after a completed write

After a verified save and session release, read [post-write-graph-review.md](post-write-graph-review.md) for comparison, consent, persisted review state and complete-case replacement.

## Templates and complete parameters

Use [targeting-and-source.md](targeting-and-source.md) for parameter summaries, exact reads and operation-template paging, then the relevant [patch schema](patch-schema.md) for operation fields.

## Repair a failed batch without starting over

Read [batch repair](batch-repair-and-performance.md#repair-a-failed-batch-without-starting-over) to repair the complete batch under its original expectations.

## Timing and reusable preparation

Read [timing and reusable preparation](batch-repair-and-performance.md#timing-and-reusable-preparation) when investigating service latency or cache reuse.
