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
  "scope_policy": "strict",
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
  `field_path`. New plans default to `scope_policy: "strict"`: at least one
  requirement is required, each requirement must have targets, and every real
  change must fall within the declared targets. An extra change becomes failed
  scope evidence and blocks approval. Include genuine required consequences in
  the scope before generation; do not widen scope merely to accept an accident.
- Explicit `scope_policy: "manual_review"` keeps extra changes unverified for
  human judgment. Use it only with explicit user basis in `source_text` or
  `change_source_text`, never as an automatic fallback. Changing policy requires
  a new `revision` and new user basis. Old reports without a policy remain
  viewable; preparing new approval requires replanning under the current policy.
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

## Check canvas configuration directly

Use optional `canvas_assertions` for fixed, data-only checks of the actual native
parameters. They are useful for C# queries whose business execution cannot be
verified by the local JS runner, and for preserving filters and outputs when
changing one parameter. Establish the expected values from the user's request
and current draft before generating the patch; never change an assertion just
to make a failed plan pass.

```json
"canvas_assertions": [
  {
    "id": "frozen-status",
    "requirement_ids": ["query-filter"],
    "target": {"group_key": "existing-group", "node_key": "existing-query", "element_key": "GetData"},
    "path": "/inputParams/conditions",
    "operator": "contains",
    "value": {"field": "status", "operator": "eq", "value": "freeze"}
  },
  {
    "id": "keep-output",
    "requirement_ids": ["query-filter"],
    "target": {"group_key": "existing-group", "node_key": "existing-query", "element_key": "GetData"},
    "path": "/outputParams",
    "operator": "unchanged"
  }
]
```

This is a schema example, not a definition of any native query component's
parameter names. Inspect the actual node first. `element_key` must be its current
`elementKey`; use the exact native JSON values, including wrappers and AND/OR
structure. Each linked requirement must match the assertion's group and node,
with overlapping field scope in its `targets`. An `equals` or `unchanged`
assertion may check an entire parent value that includes the targeted field.
Requirement `field_path` uses the existing native path
format such as `paramsValue/inputParams`; assertion `path` is a JSON pointer
relative to `paramsValue`, restricted to `/inputParams` or `/outputParams`.
Escape literal `~` and `/` in pointer tokens as `~0` and `~1`; array indices are
zero-based decimal numbers.

- `equals` requires `value` and compares the entire value with exact JSON types.
- `contains` requires `value` and checks one exact member of an array. It does
  not match partial objects, substrings or predicates. Check an entire filter
  with `equals`, or preserve its surrounding structure with `unchanged`, when
  AND/OR structure matters.
- `unchanged` forbids `value` and compares against the session baseline. A missing
  baseline stays unverified. Missing targets, changed component/code types and
  missing candidate parameter paths fail; missing baseline paths also fail for
  `unchanged`. JSON null is different from an absent path.

At most 100 assertions are allowed, within the overall 2 MiB review specification.
JSON nesting is bounded; non-finite numbers and executable expressions are not
supported. A changed or removed existing assertion, including its requirement
links, needs a new `revision` and new user evidence in `source_text` or
`change_source_text`. Omitted review specifications inherit these assertions.

Reports include `canvas_checks` only when this optional specification is present.
Each check shows its target, expected value where applicable, before/after
presence and values, status and reason. The report binds these checks to the
specification, baseline and candidate hashes; planning and approval recompute
against the real canvases. Any failed configuration check blocks approval and
write. Linked requirements receive `configuration_status`,
`configuration_check_ids` and `configuration_reason`. A requirement's
configuration passes only when every declared target has an explicit node and
field path covered by an `equals` or `unchanged` check at that path or a parent
path. Broad group/node targets, partly covered target lists and `contains` checks
alone stay unverified with “关联断言已通过；配置范围尚未完整核对”. Check failures
still fail the configuration regardless of coverage. These fields do not
promote the requirement's execution `status`. “配置已核对” can therefore coexist
with “运行待验证”. Neither result claims a browser or backend run.

## Read results and route the next step

The MCP calls `plan_canvas_patch`, `plan_reference_repairs` and `prepare_write`
default to `response_detail="summary"`. Planning returns
`plan_id`, `review_status`, `review_summary`, `ready_for_approval`, `report_id`,
`report_sha256`, up to five `review_issues`, `additional_issue_count` and
`evidence_tool="read_plan_review"`. Status values are `passed`, `failed`,
`unverified`. This response projection does not change the complete signed
report, validation or review UI. Request `response_detail="full"` only when the
complete returned report is necessary; it adds the former `review_report`.

Read details with `read_plan_review(plan_id, section, offset=0, limit=10,
item_id="", conversation_id=...)`. Sections are `summary`, `requirements`,
`changes`, `scenarios`, `canvas_checks`, `spec`. Limits are 1–20 items. Select an
issue's section and ID when available; otherwise use its index as the offset.
Follow `next_offset` because the 256 KiB page budget can shorten a page. If one
item alone exceeds that budget, inspect the complete evidence in the formal
approval window when eligible. A standalone `open_plan_review` window is only
for an explicit user request; do not launch it automatically for oversized
evidence or before approval. Do not interpret an empty oversized page as no evidence.

`no_changes=true` means the requested state already matches: no new plan or
approval was created. Stop that planning path without `prepare_write`.
An existing plan is retained, not replaced or cancelled; inspect the returned
`existing_plan_id` and `existing_plan_status` if present. Do not automatically
prepare that older candidate merely because the latest request made no changes.
Compiler errors expose `error_details` when available. Use the reported operation,
target and reason to repair the specific mismatch; a compiler error is not a
completed blocked business plan and does not create an approval transaction.

- **Failed:** a candidate result contradicts an established expectation. The
  plan exists in blocked state. Use `review_issues` and `read_plan_review` to
  locate the mismatch and correct operations under the same expectations.
  Do not open a preview or approval window for this repair loop.
- **Unverified:** missing expectations, unsupported APIs, unknown SDK behavior,
  unavailable runner or timeout. Show the reason and remaining manual check;
  human review remains available. Never describe this as passed.
- **Passed:** the specified cases passed in the stated model. This establishes
  neither complete branch coverage nor live browser/backend behavior.

When the plan is ready and write gates pass, call `prepare_write` directly.
The formal approval window contains the comparison and evidence; a separate
preview step adds no approval and is omitted from the normal workflow.

The tab shows requirements and source text, before/after differences, affected
locations and side-by-side scenario results. Select a row to inspect details and
locate its canvas node. “受上级条件影响” is static impact, not proof that a branch ran.
On report entry, failures select the first failed configuration, then candidate
scenario, then scope change, in that priority order. Manual tab and scenario
choices remain under the reviewer's control after loading.

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
