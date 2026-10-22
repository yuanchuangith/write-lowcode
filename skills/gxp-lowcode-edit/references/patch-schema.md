# Canvas patch schema

All operations edit one existing unpublished ActionDesign. Creating a child action group does not create a new design row. The compiler generates group, parameter and node keys; callers never supply new persistent keys or generated JS/C#.

## Capability discovery

Check `supported_operations` from session entry or the latest readiness evidence before planning extended operations; call `editor_status` only if that evidence is missing. If the requested operation is absent, reload the updated MCP plugin. Do not substitute arbitrary JSON or SQL. Read inspect_component_contract(component_type) for the compiled component's event names and callback input/output declarations. This query does not verify a component instance on a page; establish its current page, ID, type and visible title through readonly page evidence first.

## Internal batch preflight

`plan_canvas_patch` preflights the entire submitted operation batch before native generation or C# compilation; do not add another model tool round trip. It checks later operations as well as the first, simulates sequential existing-parameter changes on a private baseline copy and validates field/path/old-value/array-selector/alias rules. Structural operations whose generated defaults or keys are not yet available make dependent checks pending; `pending_checks` does not certify native validity.

Failures expose bounded `error_details.issues`, `issue_count`, `issues_truncated`, `pending_checks`, a zero-based `operation_index`, trusted group/node/path, `reason_code`, `expected_shape` and `refresh_scope`. Correct the shown independent errors and resubmit the complete batch; if truncated, subsequent bounded results may reveal remaining errors. Preserve expectations and resubmit from the immutable session baseline, never append operations to a failed candidate or approve an older plan automatically. See [evidence-and-performance.md](evidence-and-performance.md) for templates and repair scope.

## Group creation

create_action_group fields:

- group_alias: unique identifier for this batch, without @.
- group_title: visible, nonblank single-line title, distinct from existing group titles.
- action_type: js or csharp.
- group_description: optional string.
- input_params / output_params: optional declaration arrays; each declaration has title (a parameter identifier), type, optional description and required. Keys are generated internally. These arrays declare group parameters, not node parameter values.
- event_contract: optional {component_type, event_name}. For a JS callback, derive its input/output declarations from the native event catalog and any source-bound audited payload contract. Incomplete contracts block binding. Do not combine this field with explicit declaration arrays.

Later operations in this batch can use @group_alias for action_group_key or target_action_group_key. Aliases are batch-local, must be declared first and are never persisted. Existing groups use their exact current keys. Returned diff entries report the generated keys and parameter declarations. Group deletion, arbitrary group metadata replacement and creating a new top-level ActionDesign are not supported.

## Repair callback declarations

set_event_callback_contract fields: action_group_key, component_type, event_name. Applies the complete verified event declaration to an existing JS callback. Preserves compatible positional input names and keys; otherwise introduces the audited default names. Does not rewrite expressions. Native regeneration and affected-binding checks must succeed.

## Nodes

- append_node: action_group_key, element_key, input_params, output_params. Adds native generated nodes at the end of the group, including an empty group, without an anchor. Paired blocks are generated and validated by the native compiler.
- insert_in_block: action_group_key, target_node_key, element_key, input_params, output_params. Appends native generated nodes inside the selected opening node's branch, immediately before its first sibling branch transition (Else/ElseIf) or matching end node. The target must be a native opening node (for example IfCondition); ordinary nodes, end/branch transition nodes and unclosed blocks are rejected. Nested blocks keep their own matching ends; targeting an IF never places the new child in its Else branch.
- insert_before / insert_after: action_group_key, anchor_node_key, element_key, input_params, output_params.
- replace_params: action_group_key, target_node_key, input_params, output_params.
- set_parameter: action_group_key, target_node_key, expected_element_key, path, expected_old_value, value.
- replace_block: action_group_key, target_node_key, element_key, input_params, output_params.
- delete_block: action_group_key, target_node_key.
- move_block: action_group_key, target_node_key, anchor_node_key, position (before or after).

Node input_params and output_params remain the actual element's parameter dictionaries. Inspect the registry and its real parameter editor; nested table fields are not interchangeable with plain ParamInput records. A block operation addresses its complete levelMarker/endMarker range. Anchors and targets must exist in the selected group. New node keys, depth, raw title/description, generated code, SQL and relation data are forbidden.

### Referencing newly generated nodes in one batch

When `supported_operations` advertises `insert_in_block`, `append_node`, `insert_before`, `insert_after` and `insert_in_block` also accept optional `node_alias`. Use a unique batch-local identifier without `@` (letters/underscores followed by letters, digits or underscores). It names the **first native generated node**, which is the opening node for a paired block. It never supplies or changes a persistent node key.

Later operations in the same batch may use `@node_alias` in `target_node_key` or `anchor_node_key`. Node and group aliases have separate namespaces. References must be declared earlier, remain in the same action group and still identify one current node; unknown, forward, deleted/replaced and cross-group references are rejected. Aliases appear in operation diffs but are not persisted in the canvas and cannot be reused in another planning call.

To build an IF with two filters in one plan, create the native `IfCondition` with `node_alias: "skillOnly"`, then issue two `insert_in_block` operations with `target_node_key: "@skillOnly"` and the exact verified `DataFilter` parameters. Both filters remain between IF and End IF in operation order. `append_node` alone still appends at group end, outside a block that already has its end node. For an existing block, pass its verified opening-node key directly. Do not save a placeholder IF merely to discover its keys; every plan starts from the immutable session baseline.

`replace_params` replaces the node's **complete** input and output parameter dictionaries; it does not merge a partial update. Omitting a section produces an empty dictionary, and omitted fields are not preserved. Read the exact target's current `paramsValue`, copy both dictionaries, and change only the requested leaves before submitting them. Keep unrelated filters, AND/OR structure, output mappings, and native parameter records intact. For example, when adding a query filter, submit all original filters plus the new filter and the unchanged output dictionary; do not submit only the new filter. Inspect the returned field diff for unintended deletions before requesting approval.

## Guarded parameter updates

Prefer `set_parameter` when changing an existing parameter. It copies the target's current input/output dictionaries, changes exactly the requested value, and uses the native `replace_params` generator. Unrelated parameters and outputs need not be repeated in the operation. This leaves the complete-replacement behavior of `replace_params` unchanged.

Required fields:

- `action_group_key`, `target_node_key`: exact current group and existing node keys (group aliases remain batch-local).
- `expected_element_key`: the target's exact native element type; a mismatch or ambiguous target blocks compilation.
- `path`: JSON pointer relative to `paramsValue`, starting with `/inputParams/` or `/outputParams/`. Every selected dictionary key must already exist. Escape `~` as `~0` and `/` as `~1`. Roots, complete input/output dictionaries, canvas metadata, malformed pointers and `__proto__`, `constructor`, or `prototype` keys are forbidden.
- `expected_old_value`: the complete current value at that path, including its exact JSON type and contents. This field is mandatory even when the value is `null`. A stale value blocks the operation.
- `value`: the replacement JSON value. Existing records keep all their dictionary keys; replacing a scalar with a container, removing record fields, or introducing dictionary keys is unsupported. Expressions stay native parameter values; callers cannot supply generated source artifacts.

Array indexes are unsupported in this operation. To add/remove/update a filter row, submit the **complete existing array parameter** as `expected_old_value` and its complete replacement as `value`. Include all unchanged rows and the intended native AND/OR structure. Other input parameters and the output dictionary remain preserved. A record inside an array cannot be addressed through its numeric position.

For known `SetVariable` (`inputParams.variableValue`, `outputParams.variableName`) and `SetVariableValue` (`inputParams.attributeValue`) ParamInput fields, submit the whole existing record, including synchronized `value` and `code` for `paramTypes: "custom"`. Changing only one member is blocked; the adapter never guesses how to synchronize arbitrary components. Updating an expression may still require updating its display metadata intentionally in that record.

Each operation checks the current value **after previous operations in the batch**. A second update of the same path must therefore expect the first update's result. The service's existing complete-draft fingerprint separately guards against concurrent draft changes. Native regeneration must preserve the complete projected parameters; an omitted empty unused dictionary is equivalent to an empty dictionary. Any other native normalization that changes an unrelated parameter blocks this operation. Inspect the real native contract and use an explicit complete replacement when coordinated changes are required. Static parameter checks and compilation do not establish runtime behavior; preserve the normal business review and human approval gates.

~~~json
{
  "operation": "set_parameter",
  "action_group_key": "<verified-group-key>",
  "target_node_key": "<verified-node-key>",
  "expected_element_key": "<verified-element-type>",
  "path": "/inputParams/status",
  "expected_old_value": "active",
  "value": "freeze"
}
~~~

The example applies only if the real element has that exact existing scalar parameter. Read its current record and native contract; do not infer parameter shapes from this example.

For an existing `SetVariable` expression, update the complete native record:

~~~json
{
  "operation": "set_parameter",
  "action_group_key": "<verified-group-key>",
  "target_node_key": "<verified-node-key>",
  "expected_element_key": "SetVariable",
  "path": "/inputParams/variableValue",
  "expected_old_value": {"paramTypes": "custom", "value": "1", "code": "1", "label": "value", "dataType": "number"},
  "value": {"paramTypes": "custom", "value": "2", "code": "2", "label": "value", "dataType": "number"}
}
~~~

Use the actual record's complete keys and values. For an array-valued filter parameter, select that array's path, use the entire current array as `expected_old_value`, and supply all retained rows plus the new row as `value`; do not select a numeric array index. Add fixed configuration assertions for the intended filter structure and unchanged output as described in [business-review.md](business-review.md).

### Existing array rows with native identities

Use `set_array_item` only when `supported_operations` advertises it and the
actual array contains a unique native `id` or `key`. It uses the same target,
element and path checks as `set_parameter`; `path` selects the existing array,
not a numeric position. Supply `item_key: {"field": "id", "value": "<native-row-id>"}`
or `field: "key"`. The identity value must be a nonblank string or safe integer
that matches exactly one existing row. Never invent a row identity.

`expected_old_value` and `value` are the **complete old and replacement row**.
Preserve all existing row fields and every native `id`/`key`; changing identity,
missing or duplicate matches, and stale old values block the operation. Other
array rows and parameters remain intact. This changes an existing identified
row; adding/removing rows still requires an explicit complete-array update.
Native generation and complete projected-parameter checks remain mandatory.

When planning returns `no_changes=true`, no new plan or approval was created;
report that the requested parameter already matches and do not prepare a write.
Any older plan and its expectations remain intact. This response does not cancel
it or authorize submitting it to approval as the result of this no-change request.

## Component event binding

bind_component_event fields:

- action_group_key: JS group where the binding executes, usually a verified initialization path.
- component_id, component_type: exact current page component identity; optional component_title for its visible label.
- event_name: an event from inspect_component_contract.
- target_action_group_key: a different JS group whose ordered input arity/types and output names/types match the event signature (input parameter names may differ when consistently declared and referenced); accepts @group_alias.
- Optional target_node_key: replace an existing ComponentEvent node in the source group while preserving its key and execution scope.
- For insertion, optionally anchor_node_key plus position (before or after). Without an anchor, append at group end; position may be omitted or append.

The adapter generates the native ComponentEvent node, ParamInput component selection and ActionSelect link. Unknown events, mismatched callback parameters, C# event bindings, self targets and duplicate bindings block the plan. To modify an existing binding, identify its exact target_node_key. Replacement cannot also specify insertion positioning. Native JS/C# generation, structural validation, approval and save verification are retained.

This operation writes an executable binding node in ActionDesign. It does not edit the page designer's own event/action property panel. The initialization path must actually execute before the event occurs; static compilation cannot prove page lifecycle execution or that a component ID exists in the deployed page.

## Example: create a click callback and bind it

First verify the page button ID and query its native event signature. Then plan one batch:

~~~json
[
  {
    "operation": "create_action_group",
    "group_alias": "submitClick",
    "group_title": "提交按钮点击",
    "action_type": "js",
    "event_contract": {"component_type": "EformButton", "event_name": "click"}
  },
  {
    "operation": "append_node",
    "action_group_key": "@submitClick",
    "element_key": "OutputLog",
    "input_params": {"params": [{"name": {"paramTypes": "custom", "value": "clicked", "code": "clicked", "label": "按钮点击"}}]},
    "output_params": {}
  },
  {
    "operation": "bind_component_event",
    "action_group_key": "main",
    "component_id": "<verified-button-id>",
    "component_type": "EformButton",
    "component_title": "提交按钮",
    "event_name": "click",
    "target_action_group_key": "@submitClick"
  }
]
~~~

The component ID and initialization group in this example must be replaced with verified current evidence. Review the generated group parameters, binding component/event/target and lifecycle location before approving the draft save. Publication and real page verification remain separate.
