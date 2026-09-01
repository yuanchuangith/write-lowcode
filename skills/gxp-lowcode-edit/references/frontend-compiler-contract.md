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

`prepare_write` parses and revalidates the stored candidate, recomputes every hash, compares the complete compiler binding, and binds the resulting evidence into the approval transaction. Failure occurs before transaction creation and before the approval application is launched.

Runtime-reference C# compilation remains a separate advisory check. It cannot replace or weaken these frontend compiler gates.
