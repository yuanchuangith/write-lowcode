# Business logic acceptance

Use this reference when constructing `review_spec`, explaining a failed plan, or
interpreting the default “业务逻辑验收” tab. It does not authorize editor activation
or replace the existing compiler, backup, ownership and human approval checks.

## Fix expectations before generating the patch

Extract rules from the current user request and previously confirmed business
rules. Preserve the user's words in `source_text`; `text` is a short explanation.
Identify the real existing group/node using current draft evidence. Include
behavior that must remain unchanged. Ask about a missing critical rule rather
than guessing what an empty value, unmatched event or return value means.

Choose scenarios that can distinguish the requested behavior from likely errors:
normal and empty inputs, each relevant branch, threshold boundaries, excluded
page modes, and sequential events where retained values or handlers matter.
Independent cases start fresh; steps inside one case share state.

## Planning protocol

Both `plan_canvas_patch` and `plan_reference_repairs` accept an optional
`review_spec`. This example assumes a verified existing `initialize` group and
`assign` node, and a user request to preserve values when editing:

```json
{
  "schema_version": 1,
  "revision": "request-1",
  "requirements": [
    {
      "id": "preserve-on-edit",
      "text": "编辑时保留原值",
      "source_text": "仅新增且页面参数非空时赋值，编辑已有记录时保留原值",
      "targets": [{"group_key": "initialize", "node_key": "assign"}]
    }
  ],
  "scenarios": {
    "schema_version": 1,
    "cases": [
      {
        "id": "edit-existing",
        "title": "编辑已有记录",
        "requirement_ids": ["preserve-on-edit"],
        "environment": {
          "page_mode": "edit",
          "fields": {"target": {"value": "原值"}}
        },
        "steps": [{"kind": "call_group", "group_key": "initialize", "args": []}],
        "expect": {"fields": {"target": {"value": "原值"}}}
      }
    ]
  }
}
```

Use the actual page-mode values established by the component contract. The
example does not define the GXP SDK's modes or certify that one scenario fully
covers the quoted request. Add the relevant creation/empty/boundary cases.

- A requirement target contains `group_key` and optionally `node_key` and
  `field_path`. Targets associate differences with requirements; extra differences
  remain visible for human review.
- Each case needs an `id`, 1–10 `steps`, and can have `title`, `requirement_ids`,
  `environment`, and `expect`. A requirement without linked executed cases stays
  unverified. There are at most 20 cases, within a 1 MiB scenario specification.
- Environment supports `page_mode`, `fields` and `child_fields`. Field fixtures
  declare `value`, `text` or `selected_rows`; undeclared reads stay unverified.
- Steps are `call_group` (`group_key`, JSON `args`), `set_inputs`
  (`fields`, `child_fields`, `page_mode`), `emit_event`
  (`component_id`, `event_name`, JSON `args`, optional `inject_inner_inbiz`),
  or `read_metadata`. Event injection is an explicit runner adapter, not a
  browser event simulation.
- Fixed expectations support `fields`, `metadata`, `metadata_registrations`,
  `last_return`, `last_return_undefined: true`, `call_counts` (API name to count)
  and `call_order` (the exact observed API sequence, such as `group.initialize`
  followed by `target.value.write`). Never submit JavaScript stubs, functions,
  predicates or executable assertion expressions. Integers must be within the
  JS safe range; represent larger business identifiers as strings.
- Omission inherits the preceding plan's specification in this session. A new
  explicit specification needs a new `revision` when changed. Changed or removed
  requirements or existing scenario inputs/steps/expectations need new user
  evidence in `source_text` or `change_source_text`; copying old evidence or only
  adding whitespace does not count. Never remove a failing scenario simply to
  obtain approval.

## Read results and route the next step

Planning returns `plan_id`, `review_status`, `review_report` and
`ready_for_approval`. Status values are `passed`, `failed`, `unverified`.

- **Failed:** a candidate result contradicts an established expectation. The
  plan exists in blocked state. Open it with `open_plan_review(plan_id)` and
  correct the operations under the same expectations. No approval or write is
  created by viewing it.
- **Unverified:** missing expectations, unsupported APIs, unknown SDK behavior,
  unavailable runner or timeout. Show the reason and remaining manual check;
  human review remains available. Never describe this as passed.
- **Passed:** the specified cases passed in the stated model. This establishes
  neither complete branch coverage nor live browser/backend behavior.

The tab shows requirements and source text, before/after differences, affected
locations and side-by-side scenario results. Select a row to inspect details and
locate its canvas node. “受上级条件影响” is static impact, not proof that a branch ran.

## Evidence boundaries

The runner executes the actual baseline and candidate JS independently, using
controlled in-memory APIs. It does not load the real SDK or connect to business
services. Each run is bounded to 2 seconds and the suite to 20 seconds. Unsupported
operations and swallowed unsupported errors cannot become passing evidence.
Metadata's `last_registration_wins` model is an explicit local assumption; SDK
registration/return-wrapper behavior remains unverified.

The report is bound to requirements, scenarios, runner version, baseline,
candidate and compiler. Replanning invalidates old approval eligibility. Existing
C# compilation and saved-draft checks keep their original meaning;
`browser_verified` and `runtime_verified` remain false for this local runner.
Old records remain viewable, but pending writes without current review evidence
must be regenerated. Restore uses its existing immutable-backup approval path.
