# Frontend compiler contract

The editor does not invent ActionDesign nodes or generated code. Its configured frontend repository is:

`G:\hoyi\updateComponents\gxp2.components`

The configured bridge is normally `dist/action-design-compiler/bridge.js`. It executes from the frontend repository and uses the real ActionDesign element registry and generation methods bundled from that codebase.

## Blocking source binding

The compiler binding contains:

- frontend Git root, branch, commit, dirty-file count, and a workspace fingerprint;
- bridge path and SHA-256;
- plugin compiler-runner and structured-operation adapter SHA-256;
- frontend version;
- ActionDesign rule-source SHA-256;
- stable element-registry SHA-256;
- a SHA-256 over the complete binding.

The working tree may be dirty, but its exact fingerprint is bound. Any binding change between session start, planning, and `prepare_write` invalidates the current workflow. Do not rebuild, reset, format, or modify the frontend repository as part of an edit request.

## Structured group and component operations

The plugin runner loads the configured native bundle with a narrow in-memory dispatch extension; it does not rewrite the frontend repository or replace native node/code generators. The recognized webpack bundle must expose the native component catalog and compiler dispatch. An unknown or ambiguous bundle shape fails closed. Both runner and adapter hashes participate in the complete compiler binding, so an upgrade invalidates old plans.

`create_action_group` follows the platform child-action shape and generates keys internally. `append_node` and `bind_component_event` call native `generateNodes`; the normal compiler still recomputes nesting, validates the complete design and generates JS/C#/relations. Event callback declarations come from the compiled native component catalog, enriched only by source-hash-bound audited runtime payload contracts. `inspect_component_contract` exposes those signatures without claiming that a page instance exists. The skill must verify current component identity and the binding execution path separately.

Approval previews show added/removed groups and group parameter changes even when their canvases are empty. Group details are display projections only and are never stored as executable nodes. The native ComponentEvent node shows the component, event and callback link in the ordinary parameter comparison.

## Blocking canvas and code checks

`plan_canvas_patch` must finish all checks before a plan file or state record is created:

1. Apply only structured operations through the bridge.
2. Require `validation.valid == true` and matching frontend/rule/registry identities.
3. Revalidate the returned candidate design through the same bridge.
4. Require `actionData` and `globalVariables` arrays plus an object `relation_data`.
5. Require both generated-code fields to exist as strings.
6. Require nonblank `js_code` when a JS action group exists and nonblank `csharp_code` when a C# action group exists.
7. Bind data, JS, C#, and relation hashes into the plan.

Static variable/action reference checks are advisory. The native bridge returns
their diagnostics in `validation.warnings` while leaving `validation.valid` true
when structural checks pass. Report warnings and continue ordinary edits; an
obsolete parameter key or unresolved action target alone does not prove runtime
failure. Native generation failures still block planning. Session entry returns
the baseline validation, and candidate warnings are preserved in the plan and
signed approval evidence.

For older installed bundles, run `python scripts/relax_compiler_references.py
<frontend_repo>/dist/action-design-compiler/bridge.js` before starting a session.
The updater changes only the five static reference diagnostics, retains all
structural and native generation checks, and rejects unrecognized bundle shapes.
Reapply it after rebuilding an older bridge. The resulting bridge SHA-256 remains
part of the compiler binding; existing sessions cannot silently adopt the change.

## Canvas visibility and re-editing

The canvas parameters are the source of truth. The platform regenerates JS/C# on
save; changing only generated code would be overwritten. Use native IF blocks
when the requested change is a visible control-flow change. A conditional expression
inside a variable element is not an added IF block; describe that distinction in
the review and give the exact component configuration field where it can be read.

For the known ParamInput fields in SetVariable (variableValue and variableName)
and SetVariableValue (attributeValue), custom input displays `value`, and input
commit writes that string to both `value` and `code`. Validation blocks missing or
different custom `value`/`code` before approval. A human-readable `label` may differ;
it is the canvas description, not the executable expression. Do not extend this
equality rule to other element contracts without inspecting their actual editors.

Report the action, group, canvas row, element name, adjacent visible elements,
configuration field, and full expression. An isolated generateActionData round trip
checks compiler preservation; it does not prove deployed browser editing behavior.

## Approval review

Component cards, the change summary, and raw node details use the same canvas
differences. Review nesting changes as well as parameters: moving a node into or
out of an IF changes its execution scope even when its expression is unchanged.
SQL navigation preserves the selected component, expanded items, and scroll
position. Unchanged canvases reuse their review model; code comparisons are
cached separately for JavaScript and C#.

Start with the component parameters and their before/after values. The code tab
still displays and copies the exact full saved/candidate code. Blue differences
are a display-only hint for consistent temporary-variable renames recognized
from unchanged SetFormReadOnly, SelectModelData, or CallAction elements and their
known declaration forms. Unknown identifiers, inconsistent references, changed
literals, and ambiguous syntax remain ordinary differences. The hint is not a
per-node source map or proof of semantic equivalence; it never changes signed
artifacts, SQL, approval requirements, or executable code.

`prepare_write` parses and revalidates the stored candidate, recomputes every hash, compares the complete compiler binding, and binds the resulting evidence into the approval transaction. Failure occurs before transaction creation and before the approval application is launched.

Runtime-reference C# compilation remains a separate advisory check. It cannot replace or weaken these frontend compiler gates.

## Legacy reference repair adapter

The Python repair adapter exposes a dedicated session and report. Ordinary
`compile_patch` treats static references as advisory; explicit repair requests
still require resolving the reported bindings. Its scope catalog follows the configured frontend's
`Components/ParamInput.tsx` (`addLocalVariable` and `handleParamTypeChange`):
same-group input/output keys and titles, global declarations, and preceding
custom output variables visible in the consumer node's depth. `SetVariable`
output types come from `variableType`, as in its `generateActionData` contract.
Other declared output types are used without coercion or fuzzy name matching.
Native registry `level_marker` flags determine block visibility.

The inspection projection changes only diagnosed structured variable records
into inert custom records for a structural validation call; it is never passed
to code generation or stored as a plan. The original snapshot is immutable.
After all candidates resolve, the adapter updates those binding fields on a copy
and calls native `validate_design`, `compile_patch` with native `replace_params`,
and the normal compiled-result verification. It rechecks structured bindings
after native regeneration and before approval. Raw generated code is never an
input. Native generation may refresh node descriptions and declared output types;
the signed approval compares the actual original and actual complete candidate.

## Event contracts and verification limits

The native event catalog can omit inputs even when a real component passes a payload. Inspect signatures before binding. Missing parameter metadata or output-only catalog entries are incomplete and block affected bindings until the runtime callsite has been audited. A catalog declaration alone remains catalog_only evidence. The audited FormTables.onChildrenCustomBeforeClick payload is one object containing id, action, name and callable innerInbiz; its default canvas input name is action. Source evidence hashes in event_contracts.json must match the configured frontend. Input names are positional and may differ (for example context) when declared and used consistently; output names remain contract keys. The audited object input also accepts the platform dictionary declaration.

For new or changed binding/callback groups, the compiler derives validation scope from actual before/after canvases, including bindings inserted through the generic native ComponentEvent element. It checks parameter arity/types, output keys, native generated signature, JavaScript syntax, lexical declarations, and forbidden callback-level arguments access. prepare_write derives and compares that evidence again. Unchanged unrelated legacy bindings are outside this gate. This does not validate all custom expression semantics or every component method/property.

set_event_callback_contract restores visible declarations for an existing JS callback; compatible existing input names/keys are preserved. It does not rewrite expressions or infer business behavior. If references still disagree, lexical checks block the candidate. Do not use it as a generated-code escape hatch.

Keep the native outputParams convention. The platform SDK useEvents/configEvent wrapper is not proven by the local callsite; do not force a primitive boolean ExitAction or claim cancellation semantics without wrapper/browser evidence. return_verification explicitly records sdk_wrapper_unverified for this event. Isolated primitive-return tests prove only the direct stubbed callsite.

Always distinguish these evidence levels in the result:

- Canvas/native regeneration and syntax/signature checks: mandatory for an affected binding.
- Business scenarios: verify expected branch, output keys/values and side effects with representative fixtures; compilation alone is insufficient. Tests may execute generated code with documented stubs, which proves only those fixtures.
- Browser/page acceptance: current component instance, initialization timing, SDK wrapping, asynchronous behavior and publication require actual page evidence. Never mark browser_verified or runtime_verified from a VM test.

The emitted validation flags business_scenarios_verified=false, browser_verified=false and runtime_verified=false are intentional: the compiler gate cannot certify these layers. Report any separately executed scenario results with their scope. Do not describe unsupported, unknown or untested combinations as confirmed usable. Many combinations are addressed through shared contract/control-flow checks and representative boundary scenarios; do not promise exhaustive correctness.

The fixture tests/fixtures/manual_child_button_callback.json preserves the user's corrected callback structure as an offline regression example. Its second evaluation-button branch still has expressions restricted to the first evaluation-button ID. The test records that observed empty result, not an independently confirmed business requirement. For unmatched IDs it checks that no new metadata handler is registered; it does not prove previous metadata is cleared.
