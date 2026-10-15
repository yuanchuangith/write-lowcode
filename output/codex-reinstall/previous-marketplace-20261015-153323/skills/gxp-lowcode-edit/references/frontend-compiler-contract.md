# Frontend compiler contract

The editor does not invent ActionDesign nodes or generated code. Its configured frontend repository is:

`G:\hoyi\updateComponents\gxp2.components`

The configured bridge is normally `dist/action-design-compiler/bridge.js`. It executes from the frontend repository and uses the real ActionDesign element registry and generation methods bundled from that codebase.

## Blocking source binding

The compiler binding contains:

- frontend Git root, branch, commit, dirty-file count, and a workspace fingerprint;
- bridge path and SHA-256;
- frontend version;
- ActionDesign rule-source SHA-256;
- stable element-registry SHA-256;
- a SHA-256 over the complete binding.

The working tree may be dirty, but its exact fingerprint is bound. Any binding change between session start, planning, and `prepare_write` invalidates the current workflow. Do not rebuild, reset, format, or modify the frontend repository as part of an edit request.

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
