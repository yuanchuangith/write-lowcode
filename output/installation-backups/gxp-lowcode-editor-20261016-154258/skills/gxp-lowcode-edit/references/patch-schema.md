# Canvas patch schema

All operations edit one existing unpublished ActionDesign. Creating a child action group does not create a new design row. The compiler generates group, parameter and node keys; callers never supply new persistent keys or generated JS/C#.

## Capability discovery

Check editor_status.compiler.supported_operations before planning extended operations. If create_action_group, append_node or bind_component_event is missing, reload the updated MCP plugin. Do not substitute arbitrary JSON or SQL. Read inspect_component_contract(component_type) for the compiled component's event names and callback input/output declarations. This query does not verify a component instance on a page; establish its current page, ID, type and visible title through readonly page evidence first.

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
- insert_before / insert_after: action_group_key, anchor_node_key, element_key, input_params, output_params.
- replace_params: action_group_key, target_node_key, input_params, output_params.
- replace_block: action_group_key, target_node_key, element_key, input_params, output_params.
- delete_block: action_group_key, target_node_key.
- move_block: action_group_key, target_node_key, anchor_node_key, position (before or after).

Node input_params and output_params remain the actual element's parameter dictionaries. Inspect the registry and its real parameter editor; nested table fields are not interchangeable with plain ParamInput records. A block operation addresses its complete levelMarker/endMarker range. Anchors and targets must exist in the selected group. New node keys, depth, raw title/description, generated code, SQL and relation data are forbidden.

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
