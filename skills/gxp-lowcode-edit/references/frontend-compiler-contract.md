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

`create_action_group` follows the platform child-action shape and generates keys internally. `append_node` and `bind_component_event` call native `generateNodes`; the normal compiler recomputes nesting and validates the complete design. Event callback declarations come from the compiled native component catalog, enriched only by source-hash-bound audited runtime payload contracts. `inspect_component_contract` exposes those signatures without claiming that a page instance exists. The skill must verify current component identity and the binding execution path separately.

`plan_canvas_patch` accepts `generation_mode="full"` (default), `"incremental"` or `"auto"` in the same plugin. Full regenerates all action groups. Incremental regenerates changed groups and transitively affected referring groups through native code/relation generators with the complete canvas as context; other groups retain their saved source text and relation data. Auto selects an actual full or incremental mode from the affected/dependency scope before code checks. Respect an explicit user mode. Without one, the agent may retain the default, choose incremental for a small scope or request auto. Report the actual mode, reason and regenerated/preserved scope before approval. A checker error, invalid event binding or compilation failure never authorizes silently retrying another mode.

Auto policy `action_groups_v1` selects incremental only with a complete saved baseline and original canvas, unchanged shared metadata, no action-type/language transition, at most 3 affected groups (including dependent and removed groups), at most 30 actually changed node keys (added/deleted/changed), and an affected-group ratio strictly below 50% of the before/after group union. Otherwise it selects full. `generation_selection` records requested/selected mode, reason code, policy version, counts, thresholds, scope and baseline/candidate fingerprints. Explicit incremental does not fall back on source-mapping failures.

Incremental generation uses the session's immutable saved baseline, not model-supplied source. Its minimum boundary is a whole action function. Shared context changes regenerate all groups; unknown or ambiguous source boundaries block rather than silently rebuilding or rewriting business logic. JS spans use Babel AST; C# spans use a conservative lexer supporting regular, verbatim and interpolated strings (raw strings currently block C# splicing). Every independent verification recomputes the actual mode and dependency scope, runs native generators, reconstructs the complete candidate and compares exact JS/C#/relations and generation evidence. `generation` records regenerated/preserved groups and baseline/candidate fingerprints; together with `generation_selection` it is bound into the plan and approval. Nonblank complete candidate C# compilation and human approval remain mandatory in every mode. Explicit reference repair retains full native generation.

All JS groups undergo syntax/lexical inspection in every mode, including unchanged, byte-preserved groups. The number of changed fields and generation scope never exempt lexical checks. Only newly introduced reachable undeclared references block. Compare each concrete reference against the immutable pre-edit saved source: match its group, identifier, normalized member/call expression and occurrence count. Location/formatting changes alone do not create a new problem. Existing reachable findings become nonblocking `baseline_diagnostics`, including when their group is regenerated; retain current/baseline locations, baseline source SHA-256, reference SHA-256, `status: "unverified"` and `blocks_changed_candidate: false`. Do not treat an existing identifier somewhere in the group as permission to add more bad references. Missing declarations introduced by this edit, added occurrences and references made reachable from a previously unreachable branch block. An unreadable or absent baseline provides no proof that a finding preexists. The normal session always supplies its immutable saved baseline; lower-level offline calls without saved artifacts derive baseline source through native generation.

Statically proven unreachable references remain in `unreachable_diagnostics` with their reference location, controlling source location, a specific reachability reason and the same unverified/nonblocking status. Neither class becomes an allowed global or runtime success. Independent verification recomputes the classification from the same baseline and complete candidate source before approval. Historical `baseline_diagnostics` lacking concrete source-match evidence require replanning. Report old findings, leave their repair to the user's decision and continue the authorized edit without another confirmation; do not silently add unrelated repairs to the current scope.

Supported automatic proofs are literal-selected IF/conditional branches, literal short-circuit expressions, false-tested while/for bodies or for updates, and statements following an unconditional return/throw in the same statement list. Literal evaluation uses only boolean/number/string/null literals and `!`; it never executes expressions. Unknown conditions remain potentially reachable. Do not carry statement reachability across nested function boundaries: a hoisted declaration after a return can still be called. No local caller, a title/comment saying deprecated, or an unexecuted test scenario proves that an action is unused: the platform, page events or dynamic/external callers may invoke it. The current compiler has no authoritative whole-page deprecation/call-graph input, so it does not automatically exempt whole groups on that basis. Preserve syntax, all reserved-name/platform-mount checks (declarations may be hoisted), event contract checks, and complete candidate C# compilation even for unreachable code. The unreachable exception covers undeclared JS references and implicit event arguments, not malformed code or generation integrity failures. Old issues in the other checks require their separate baseline evidence.

Report located unreachable findings and their evidence without requiring unrelated repairs; do not change old business expressions or callback names merely to satisfy the tool. Preserved `eval(...)` remains source only; dynamic-code scenarios remain unverified when the scenario runner cannot execute them. Saved relation changes invalidate planning/approval even if the design row is unchanged.

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

## Reusable preparation and timings

Preparation may be reused only under its verified source/SDK/reference binding;
candidate success is never cached. Fresh independent generation, complete
candidate compilation before planning and approval, source/reference checks and
timeouts retain their blocking meaning. Cache identity, workspace cleanup,
cold/warm failure handling and interpretation of `trace_id`, `total_elapsed_ms`
and `timings` are maintained in [timing and reusable preparation](batch-repair-and-performance.md#timing-and-reusable-preparation).

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

The native event catalog can omit inputs even when a real component passes a payload. Inspect signatures before binding. Missing parameter metadata or output-only catalog entries are incomplete and block new or changed bindings until the runtime callsite has been audited; unchanged legacy declarations use the baseline classification below. A catalog declaration alone remains catalog_only evidence. The audited FormTables.onChildrenCustomBeforeClick payload is one object containing id, action, name and callable innerInbiz; its default canvas input name is action. Source evidence hashes in event_contracts.json must match the configured frontend. Input names are positional and may differ (for example context) when declared and used consistently; output names remain contract keys. The audited object input also accepts the platform dictionary declaration.

For new or changed binding/callback groups, the compiler derives validation scope from actual before/after canvases, including bindings inserted through the generic native ComponentEvent element. It checks parameter arity/types, output keys and native generated signature for affected callbacks. A callback-body edit does not by itself make an existing declaration mismatch new. If the baseline has the same binding node, component/event/target selection and complete callback declarations, retain parameter mismatches as `parameter_issues` with `preexisting_callback_parameter_mismatch`, matching baseline/candidate declaration hashes and unverified/nonblocking status. Report them for the user's decision and continue the current edit without automatically changing parameter names, types or references. In particular, catalog `any` versus existing `string` metadata is not a JavaScript runtime type error or proof that the current value is not a string. New/rebound events and changed declarations still fail on incompatible arity/types/outputs. Missing targets, unknown contracts and generated-signature failures remain separate checks.

JavaScript syntax, lexical declarations and callback-level `arguments` are checked for all JS groups (including existing lower-level groups). Existing implicit arguments and reserved declarations/writes require matching AST occurrence counts and baseline source fingerprints; they are reported in `baseline_diagnostics`, not repaired automatically. Changed write expressions, additional occurrences, new binding contexts and newly reachable implicit arguments still block. Reserved declarations remain checked in dead branches because they may be hoisted. `prepare_write` independently derives and compares the complete evidence again. This does not validate all custom expression semantics or every component method/property.

For a scoped existing event whose component identity, contract availability or callback target cannot be verified, `checks[].binding_issue` records the precise reason, node, `unverified` status and equal pre-edit/candidate declaration fingerprints. Only an unchanged complete binding and unchanged source/target declarations qualify; new or changed bindings still block. Only identified missing/incomplete catalog declarations qualify as contract availability issues; audited source hash drift, ambiguous catalogs and unexpected checker failures still block. No unavailable contract is reported as matched or runtime verified. Native syntax, missing generated functions and generated/canvas signature disagreement remain generation integrity failures.

Known ParamInput display/code inconsistencies are scanned in every candidate. An unchanged complete field at the same group/node/element/path is recorded in `canvas_diagnostics`, with its complete value and equal baseline/candidate hashes; new or changed inconsistent fields block. Session entry reports old inconsistencies without requiring unrelated repair. The same evidence format covers old reserved global/output declarations. Editing a ParamInput still requires submitting its complete synchronized record.

`inbiz` and `Inbiz` are exact reserved business variable names. The gate rejects JS canvas global/input/output names and generated local/nested bindings using either name, as well as direct assignments/updates to those identifiers or the corresponding `window`/`globalThis` mounts. The native factory parameter `inbiz` and normal platform API reads/calls are preserved. Distinct variables such as `innerInbiz` and `pageInbiz`, payload properties such as `action.innerInbiz`, and ordinary object keys/string values do not collide. This static check does not prove the absence of indirect writes through aliases or dynamic code; it never renames existing business variables automatically.

set_event_callback_contract restores visible declarations for an existing JS callback when that repair is within the user's requested scope; compatible existing input names/keys are preserved. Do not invoke it solely to silence a preexisting event metadata mismatch. It does not rewrite expressions or infer business behavior. Newly introduced unresolved references still block the candidate. Do not use it as a generated-code escape hatch.

Keep the native outputParams convention. The platform SDK useEvents/configEvent wrapper is not proven by the local callsite; do not force a primitive boolean ExitAction or claim cancellation semantics without wrapper/browser evidence. return_verification explicitly records sdk_wrapper_unverified for this event. Isolated primitive-return tests prove only the direct stubbed callsite.

Always distinguish these evidence levels in the result:

- Canvas/native regeneration and syntax/signature checks: mandatory for an affected binding.
- Business scenarios: verify expected branch, output keys/values and side effects with representative fixtures; compilation alone is insufficient. Tests may execute generated code with documented stubs, which proves only those fixtures.
- Browser/page acceptance: current component instance, initialization timing, SDK wrapping, asynchronous behavior and publication require actual page evidence. Never mark browser_verified or runtime_verified from a VM test.

The emitted validation flags business_scenarios_verified=false, browser_verified=false and runtime_verified=false are intentional: the compiler gate cannot certify these layers. Report any separately executed scenario results with their scope. Do not describe unsupported, unknown or untested combinations as confirmed usable. Many combinations are addressed through shared contract/control-flow checks and representative boundary scenarios; do not promise exhaustive correctness.

The fixture tests/fixtures/manual_child_button_callback.json preserves the user's corrected callback structure as an offline regression example. Its second evaluation-button branch still has expressions restricted to the first evaluation-button ID. The test records that observed empty result, not an independently confirmed business requirement. For unmatched IDs it checks that no new metadata handler is registered; it does not prove previous metadata is cleared.

## Exact generated artifact consistency

Both candidate generation verification and prepare_write independently recompute the selected generation mode/scope on the candidate canvas, invoke native compileCode and reconstruct the complete candidate, requiring exact JS, C# and relation-data equality. The signed evidence includes generated_code_consistency_verified=true; the transaction validator requires it. This catches syntactically valid artifacts that no longer correspond to their canvas.

Native compile-time UUIDs used for temporary symbols/configuration IDs are reproducible from a canonical canvas hash and call counter only while compileCode runs. Node creation still uses native random UUIDs. The adapter never rewrites business strings, generated source text or runtime random expressions. An unrecognized bundle/stub shape blocks compilation.

Local C# checks use bounded subprocesses. Nonblank C# cannot proceed after unavailable/failed checks. SDK/reference status is only a capability probe, so editor_status never claims a candidate or production has been verified. Production DLL snapshots and real page acceptance are separate evidence; never relabel a development build as a deployment snapshot.
