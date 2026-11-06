# 表单配置操作契约

使用 `gxp-form-config-editor` 返回的组件目录和当前 MCP schema。允许路径、值类型及原生适配器来自服务端契约；不要把这里的分类当作每个组件都支持全部属性的保证。

每项操作具有以下字段：

```json
{
  "operation": "set_property",
  "component_id": "工具确认的现有组件 ID",
  "expected_component_type": "工具确认的组件类型",
  "path": "/工具返回的组件内部路径",
  "expected_old_value": false,
  "value": true
}
```

示例仅说明结构，不能原样提交。先通过 `find_form_components`、`inspect_form_component` 和必要的 `read_form_value` 确认真实身份、路径和完整旧值。

| 操作 | 用途 |
| --- | --- |
| `set_property` | 组件允许的属性，含适配器已声明的必填、只读等属性 |
| `set_default` | 默认值配置 |
| `set_layout` | 当前组件允许的布局属性 |
| `set_binding` | 现有组件的数据源或字段绑定配置 |
| `set_filter` | 数据源中允许修改的过滤配置 |

`path` 为组件内部 JSON Pointer，`~` 写作 `~0`，`/` 写作 `~1`。只能修改目录明确允许、且适配器能处理的路径；不要提交整页替换、组件 ID/类型修改、任意脚本、SQL、URL 或自造保存请求。数据源配置使用该组件已验证的原生结构，不把动作参数格式套入组件配置。

旧值必须完整，包括对象/数组的所有成员及显式 `null`；展示摘要不能当作旧值。当前契约使用 `expected_old_value`，没有动作编辑器的 `guard_ref` 替代模式。

只有目录允许设置原先不存在的属性时，才额外使用 `expected_old_exists=false` 与 `expected_old_value=null`；省略该字段表示属性必须存在。`platform` 如有指定，必须与绑定目录及页面平台一致。数组使用完整旧值和新值，不按数字索引修改。

数据源、字典及过滤适配器可能要求经过核实的资源目录。`set_binding` 使用该适配器要求的资源 ID 和版本，让服务构造原生绑定值；缺少资源证据时不能猜造模型、数据集、字典或展示字段。

同一页的多项修改放入 `plan_form_patch`，按返回的诊断处理路径、身份、旧值或类型错误。重复修改同一路径、跨字段联动和数组行为应按当前适配器及诊断处理，不能假设动作编排器的顺序和数组语义适用。

`inspect_form_component` 默认返回摘要，必要时可用 `response_detail="full"` 读取有界完整配置；大型字段通过 `read_form_value` 读取。绑定资源先用 `inspect_form_resource` 获取原生 ID、字段及版本。字典版本可能是内容指纹，只用于引用一致性，不能当作页面服务端并发戳。

`read_form_plan` 支持分页读取属性 `changes` 与保存 `requests`；大型记录默认只显示摘要，同一记录用 `offset`、`value_path`、`value_offset` 及 `max_chars` 续读字段。核对组件、路径、前后值及请求目标；未知字段和页面原有的无关配置应保留。工具对受支持路径的验证不证明整页渲染或业务行为正确，保存后仍需设计器核对。
