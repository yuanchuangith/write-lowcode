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

`create_action_group` follows the platform child-action shape and generates keys internally. `append_node` and `bind_component_event` call native `generateNodes`; the normal compiler recomputes nesting and validates the complete design. Ordinary edits regenerate changed action groups and transitively affected referring groups through native code/relation generators with the complete canvas as context. Other groups retain their saved source text and relation data. Event callback declarations come from the compiled native component catalog, enriched only by source-hash-bound audited runtime payload contracts. `inspect_component_contract` exposes those signatures without claiming that a page instance exists. The skill must verify current component identity and the binding execution path separately.

Incremental generation uses the session's immutable saved baseline, not model-supplied source. Its minimum boundary is a whole action function. Shared context changes regenerate all groups; unknown or ambiguous source boundaries block rather than silently rebuilding or rewriting business logic. JS spans use Babel AST; C# spans use a conservative lexer supporting regular, verbatim and interpolated strings (raw strings currently block C# splicing). Every independent verification recomputes the dependency scope, runs native generators, reconstructs the complete candidate and compares exact JS/C#/relations and generation evidence. `generation` records regenerated/preserved groups and baseline/candidate fingerprints and is bound into the plan and approval. Nonblank complete candidate C# compilation and human approval remain mandatory. Explicit reference repair retains full native generation.

All JS groups still undergo syntax/lexical inspection. An undeclared identifier in an unchanged, byte-preserved group is reported under `baseline_diagnostics` as `unverified`; it does not block the local edit or become an allowed global. Regenerated groups' undeclared identifiers and all reserved-name/platform-mount violations still block. Do not change old business expressions or callback names just to satisfy the tool. Preserved `eval(...)` remains source only; dynamic-code scenarios remain unverified when the scenario runner cannot execute them. Saved relation changes invalidate incremental planning/approval even if the design row is unchanged.

Approval previews show added/removed groups and group parameter changes even when their canvases are empty. Group details are display projections only and are never stored as executable nodes. The native ComponentEvent node shows the component, event and callback link in the ordinary parameter comparison.

Native `CallInterface.generateJsCode` synchronously looks up the interface URL while generating source. The offline runner resolves only its metadata GET from the matching `apiInfo.setting.id/requestUrl` already saved in the canvas (legacy flat `apiInfo` is supported). It never contacts the interface service or executes the generated request. Missing or conflicting saved URLs, unknown metadata requests and all other synchronous network calls block generation. The generated runtime request still comes from the native generator. `compiler_evidence.interface_metadata` records the saved snapshot hash and `live_metadata_checked=false`; this proves consistency with the saved settings, not freshness against the current interface catalog. Refresh a stale/missing interface configuration through the platform before replanning; never invent a URL or a successful response.

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

## Rebuilding a stale compiler bridge

An old bridge can omit elements that already exist in the frontend source. For
example, the old 95-element bundle omitted SyncLock, SyncLockEnd, AddChangeData,
VersionFormat, FileRelease, FileCopyVersion and FileEditingCheck. Do not remove
these nodes, invent generators, or downgrade Unknown elementKey to a warning.

Compiler maintenance is separate from a business draft edit. Rebuild only when
the bridge is missing or rejected for stale source/manifest or incomplete native
registry coverage; a normal new edit session does not require rebuilding. For
that maintenance, before starting a new session, run
`node scripts/build_compiler_bridge.cjs <frontend_repo>` from this plugin to rebuild
the offline bridge from the complete native registry, node generators, code
generators and relation generators. The optional second
argument stages the build in another directory; compiler acceptance still
requires the bridge to reside within the configured frontend Git root.

The builder emits a source manifest in the bridge header and source-manifest.json.
The Python bridge checks dependency hashes and the complete native rule file set
before every compiler command. Changed, added or removed native rules and legacy
bundles without a manifest require an explicit rebuild. New builds already treat
static reference diagnostics as advisory; they do not need the legacy
relax_compiler_references.py updater. Unknown elements, unsupported languages,
broken structure and native generation failures still block the workflow.
Missing frontend sources also block verification. Cached registry reads recheck
source hashes and the bridge identity instead of retaining an obsolete binding.

Replacing the bridge changes its binding and invalidates existing plans. Restart
the editor MCP after updating plugin code and start a new validated plan; never
carry an old approval across a compiler update. Rebuilding does not save or
publish any business draft.

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

Nonblank candidate C# compilation against local read-only references is also mandatory before planning and approval. Its UTF-8 subprocess protocol preserves the exact candidate hash. Timeout, protocol errors, missing SDK/reference packs and C# errors are reported separately and block the candidate; local success does not prove production runtime behavior.

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

Lexical checks recognize the configured frontend's `globals.builtin` and browser globals, including legacy `eval(...)` expressions, without rewriting those expressions. Identifiers inside their arguments must still be declared. This only establishes lexical compatibility: the separate scenario runner continues to refuse dynamic code execution, reports affected scenarios as `unverified`, and must not claim business or runtime verification from lexical acceptance.

The native event catalog can omit inputs even when a real component passes a payload. Inspect signatures before binding. Missing parameter metadata or output-only catalog entries are incomplete and block affected bindings until the runtime callsite has been audited. A catalog declaration alone remains catalog_only evidence. The audited FormTables.onChildrenCustomBeforeClick payload is one object containing id, action, name and callable innerInbiz; its default canvas input name is action. Source evidence hashes in event_contracts.json must match the configured frontend. Input names are positional and may differ (for example context) when declared and used consistently; output names remain contract keys. The audited object input also accepts the platform dictionary declaration.

For new or changed binding/callback groups, the compiler derives validation scope from actual before/after canvases, including bindings inserted through the generic native ComponentEvent element. It checks parameter arity/types, output keys and native generated signature for affected callbacks. JavaScript syntax and lexical declarations are checked for all JS groups (including existing lower-level groups); callback-level arguments access remains forbidden for affected event targets. prepare_write derives and compares that evidence again. Unchanged unrelated legacy bindings are outside this gate. This does not validate all custom expression semantics or every component method/property.

`inbiz` and `Inbiz` are exact reserved business variable names. The gate rejects JS canvas global/input/output names and generated local/nested bindings using either name, as well as direct assignments/updates to those identifiers or the corresponding `window`/`globalThis` mounts. The native factory parameter `inbiz` and normal platform API reads/calls are preserved. Distinct variables such as `innerInbiz` and `pageInbiz`, payload properties such as `action.innerInbiz`, and ordinary object keys/string values do not collide. This static check does not prove the absence of indirect writes through aliases or dynamic code; it never renames existing business variables automatically.

set_event_callback_contract restores visible declarations for an existing JS callback; compatible existing input names/keys are preserved. It does not rewrite expressions or infer business behavior. If references still disagree, lexical checks block the candidate. Do not use it as a generated-code escape hatch.

Keep the native outputParams convention. The platform SDK useEvents/configEvent wrapper is not proven by the local callsite; do not force a primitive boolean ExitAction or claim cancellation semantics without wrapper/browser evidence. return_verification explicitly records sdk_wrapper_unverified for this event. Isolated primitive-return tests prove only the direct stubbed callsite.

Always distinguish these evidence levels in the result:

- Canvas/native regeneration and syntax/signature checks: mandatory for an affected binding.
- Business scenarios: verify expected branch, output keys/values and side effects with representative fixtures; compilation alone is insufficient. Tests may execute generated code with documented stubs, which proves only those fixtures.
- Browser/page acceptance: current component instance, initialization timing, SDK wrapping, asynchronous behavior and publication require actual page evidence. Never mark browser_verified or runtime_verified from a VM test.

The emitted validation flags business_scenarios_verified=false, browser_verified=false and runtime_verified=false are intentional: the compiler gate cannot certify these layers. Report any separately executed scenario results with their scope. Do not describe unsupported, unknown or untested combinations as confirmed usable. Many combinations are addressed through shared contract/control-flow checks and representative boundary scenarios; do not promise exhaustive correctness.

The fixture tests/fixtures/manual_child_button_callback.json preserves the user's corrected callback structure as an offline regression example. Its second evaluation-button branch still has expressions restricted to the first evaluation-button ID. The test records that observed empty result, not an independently confirmed business requirement. For unmatched IDs it checks that no new metadata handler is registered; it does not prove previous metadata is cleared.

## Exact generated artifact consistency

Both candidate generation verification and prepare_write invoke native compileCode again on the candidate canvas and require exact JS, C# and relation-data equality. The signed evidence includes generated_code_consistency_verified=true; the transaction validator requires it. This catches syntactically valid artifacts that no longer correspond to their canvas.

Native compile-time UUIDs used for temporary symbols/configuration IDs are reproducible from a canonical canvas hash and call counter only while compileCode runs. Node creation still uses native random UUIDs. The adapter never rewrites business strings, generated source text or runtime random expressions. An unrecognized bundle/stub shape blocks compilation.

Local C# checks use bounded subprocesses. Nonblank C# cannot proceed after unavailable/failed checks. SDK/reference status is only a capability probe, so editor_status never claims a candidate or production has been verified. Production DLL snapshots and real page acceptance are separate evidence; never relabel a development build as a deployment snapshot.
