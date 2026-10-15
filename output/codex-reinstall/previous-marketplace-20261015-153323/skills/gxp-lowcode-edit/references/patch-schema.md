# Canvas patch schema

Each operation has operation, action_group_key, and fields required by its operation type.

Allowed operations:

- insert_before / insert_after: anchor_node_key, element_key, input_params, output_params.
- replace_params: target_node_key, input_params, output_params.
- replace_block: target_node_key, element_key, input_params, output_params.
- delete_block: target_node_key.
- move_block: target_node_key, anchor_node_key, position where position is before or after.

Rules:

- Do not provide a key for a new node. The compiler creates all keys, including paired end markers.
- Do not provide depth, title, description, JS, C#, relation data, raw SQL, or design metadata.
- A block operation addresses the complete levelMarker/endMarker range.
- The compiler rejects anchors and targets that do not exist in the selected action group.
- All current target element keys must exist in the compiler registry before any operation is applied.
