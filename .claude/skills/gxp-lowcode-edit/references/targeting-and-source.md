# 目标元件定位与源码仓库

## 怎样确定要修改的元件

源码仓库说明“元件怎样编辑和生成代码”；当前 ActionDesign 说明“哪个动作的哪个元件需要改”。不能只根据元件名称、字段名称或仓库搜索结果决定修改位置。

1. 从用户问题或已确认诊断锁定业务规则、动作 code/RefId、页面以及字段。优先复用本对话已有的当前草稿定位证据，包含草稿设计 ID、内容指纹、分组/节点 Key 和完整目标参数。Write 可独立开始编辑会话，以其不可变备份定位当前草稿；需要发布/草稿差异或运行问题证据时，再用可用的只读 MCP `get_design_versions`、`inspect_action`。运行问题以发布副本取证，修改目标只能是当前草稿，发布副本不能代替草稿定位证据。
2. 不知道精确 Key 时，在会话内调用 `find_edit_nodes(session_id, query='', group_key='', element_key='', kind='nodes', offset=0, limit=30, conversation_id)`。它从已绑定备份返回有界 `items`，含分组/节点 Key、名称、行号及深度等定位摘要，不返回参数；按业务标签、Key 或 `element_key` 缩小范围，按 `next_offset` 翻页。用 `kind='groups'` 可列分组，包括空分组。摘要不是完整参数或业务结论，同名候选不能默认取第一个。记录动作名称/code、RefId、设计 ID、分组名称/Key、画布行/索引、节点名称/Key 和 `elementKey`；把相关候选缩小到最多 10 个精确节点后再核对完整参数。
3. 沿字段的读取、条件和赋值找到故障点；涉及嵌套或分支归属且现有证据尚未确认时，使用只读 MCP 的 `inspect_control_flow` 核对父条件和闭合块。未经本次会话备份核对的旧诊断仅作线索，不能代替当前副本证据。
4. 开始编辑会话后，使用 `inspect_edit_context(session_id, targets=[{group_key,node_key}], conversation_id)` 一次核对 1–10 个精确节点。工具检查会话归属、当前草稿指纹、关系和编译器绑定；默认摘要返回精确身份、位置、父级摘要、参数摘要和安全模板。已知参数路径时用 `parameter_paths` 聚焦。根据下节按需读取完整值或完整上下文；缺项或变化时只重取相关证据。注册表未解析动态生成和参数编辑函数体，需要源码证据的部分仍另行取证。复用结果不代替审批前实时检查。
5. 保留现有节点时，用当前 `node_key` 作为 `target_node_key`；插入时用已核对的相邻节点作为 `anchor_node_key`。`action_group_key` 来自当前草稿，新增元件的 `element_key` 来自真实注册表，不能臆造 Key 或参数结构。
6. 给用户展示可见位置、修改前后规则以及实际操作方式。若采用原元件中的自定义表达式，明确表达式所在配置字段；不能描述为“新增 IF 元件”。编译成功证明结构和生成规则可接受，不能证明业务定位正确。

`find_edit_nodes` 与 `inspect_edit_context` 都检查会话归属、当前草稿指纹和编译器绑定；搜索结果的 `context_binding` 不替代完整参数，也不能代替审批前检查。精确定位后复用已有匹配上下文。只读 MCP 不可用时，仍可通过这两个 Write 工具完成草稿定位；若会话备份和现有证据也不能提供必要内容，保留已确认结论、说明缺少的证据，不根据源码猜测业务节点。互不依赖的页面、元件契约和源码读取可并行；同一草稿的已确认操作汇总为一次计划。此取证流程不会自动从 debug 技能进入编辑；仍遵守编辑技能的用户授权和审批流程。

`begin_edit_session` 已返回 `supported_operations`、`source_repositories` 和 `readiness_snapshot`，无需为相同证据再调用 `editor_status`。状态工具默认 `probe_runtime=false`；只有排查 SDK 或引用可用性时才显式使用 `true`，候选 C# 的强制编译检查不受此选项影响。`inspect_edit_context` 只读取已确认目标，不用于搜索未知节点；先用 `find_edit_nodes` 或已有只读定位证据缩小范围。

只读工具已导出交接时，向 `begin_edit_session` 传 `handoff_ref`；匹配返回的 `edit_context` 可直接复用，不重复检查相同目标。环境、草稿及目标不匹配或发布／草稿参数有差异时只补查返回位置；失效交接不会建立新占用。跨对话明确选择记录并确认本次范围，不继承旧会话或审批，仍须显式启用编辑技能。详见 [evidence-and-performance.md](evidence-and-performance.md)。

## 参数摘要、完整值与模板

MCP 入口和 `inspect_edit_context` 默认 `response_detail="summary"`。摘要不含完整 `params_value`、父参数、可见变量或原生契约；保留定位、契约计数和按需读取提示。`parameter_paths` 可指定 1–32 个不重复的现有 JSON pointer；路径必须在每个目标中存在，不支持数组下标。需要契约、可见变量或完整替换字典时显式请求 `response_detail="full"`。显式 `include_context=true` 优先返回完整上下文，`false` 优先返回旧式完整原值 guards，两者都优先于 detail。

`edit_options.parameter_operations` 是未填完的安全模板，按 `required_inputs` 填入意图明确的替换值。摘要模板的 `guard_ref` 绑定本对话、会话修订、备份、草稿、关系、编译器、目标、路径及原值指纹；直接原样保留，可替代 `expected_old_value`。同一个操作只能选其一。服务先将签名引用还原为完整旧值，再执行原有严格预检、原生生成和审批校验。引用不代表已阅读业务内容或授予审批。

需要决策所需的完整值时，调用返回的 `read_edit_value(session_id, group_key, node_key, path, conversation_id)` 提示；读取原生数组行时传相同 `item_key`。值小时返回 `complete=true` 及精确 `value`；值大时返回规范 JSON 文本 `text` 片段、`value_sha256` 和 `next_offset`。`offset` 与 `next_offset` 按 Unicode 码点计数，中文和 emoji 不按 UTF-8 字节计数。`max_output_bytes` 是 UTF-8 输出预算（4096–65536）；服务响应中的 guard/binding 计入预算，MCP 包装和 timings 另计。所有页的 `context_binding` 和值指纹必须相同；按 next_offset 顺序拼接 text 后解析完整 JSON。最后一个片段仍 `complete=false,truncated=true`，只以 `continuation_complete=true` 表示文本收集结束。不能把摘要、preview、片段或 guard 引用对象当作完整旧值。

`operation_cursor` 只分页模板，摘要每页最多 8 条，完整模式每页最多 64 条。有 `next_operation_cursor` 时以相同有序 targets 和绑定继续；绑定改变时刷新。旧式 compact 模板的 `expected_old_value_ref.guard_index` 仍指向 `parameter_guards[index].value`，复制其完整 JSON 值。省略的上下文不是空参数；响应超过 1 MiB 应缩小目标集。模板与预检共用操作契约，不代表元件源码函数体或运行验证已检查。

`guard_ref` 只引用不可变基线。批内前一个操作修改同一路径后，下一操作必须使用前一个结果作为完整 `expected_old_value`；重复使用基线引用仍会被严格预检拒绝。数组行、原生 ParamInput 完整记录和全量替换约束见 [patch-schema.md](patch-schema.md#guarded-parameter-updates)。

`read_plan_review` 默认 `detail="summary"`，返回分页行摘要及 `field_path`。检查某个证据时固定 section 和 item_id（无 ID 时固定行 offset），用 field_path 与 `value_offset` 继续读取精确字段；字符偏移和片段规则同上。完整行兼容读取使用 `detail="full"`。响应投影不改变计划、业务复核或人工审批中的完整证据。

## 与只读技能一致的仓库

当前选定的前后端源码与 `gxp-lowcode-debug` 一致：

| 层 | 编辑器配置字段 | 默认路径 | 用途 |
| --- | --- | --- | --- |
| 前端 | `frontend_repo` | `G:\hoyi\updateComponents\gxp2.components` | 元件参数、编辑控件、代码生成；真实画布编译器绑定 |
| 后端 | `backend_repo` | `G:\hoyi\updateWeb\gxp2.web` | API、Controller、Service、保存与发布实现的只读证据 |

只读技能还保留 `F:\cpm\gxp2.components` 和 `F:\cpm\gxp2.web` 候选。需要源码取证且尚无匹配证据时，通过其 `source_repository_status` 核对实际选定路径、commit、dirty 和索引状态，再与编辑工具返回的配置路径比较；缺少配置状态时才调用 `editor_status`。同一对话已核对且路径、commit/workspace 指纹和编译器绑定未变时复用结果。路径按本机规则规范化比较；出现不同副本、歧义或缺失时，不能静默切换仓库或自动改安装配置。后端路径仅供源码取证，不纳入前端编译器绑定，也不替代 `runtime_references.directory` 的 DLL 引用校验。

## 按锚点读取源码

- 优先使用只读 MCP 的 `inspect_component_source`、`trace_api_contract`、`trace_backend_call_chain` 等结构化工具。ActionDesign 编排元件可能未被页面组件索引覆盖；`not_found` 不等于元件不存在。
- 编排元件按确切 `elementKey` 在前端 `src/core/common/ActionDesign/Element` 内定位；读取元件的 `inputParams`、`outputParams`、`generateActionData`、`generateJsCode`/`generateCsharpCode`，再沿精确 import 查编辑控件。需要参数知识时按需读取 `get_cpm_knowledge` 的单个主题。
- 例如已确认 `SetVariable` 后，读取 `Element/DataProcessing/SetVariable.tsx` 及它实际引用的 `Components/ParamInput.tsx`；不要把一个元件的参数格式套用到其它元件。
- 前端实际 API 或真实后端栈给出精确锚点后才查后端；不能凭普通字段名或表名扫描后端仓库。
- 索引未覆盖时，只在选定源码目录做有界 `rg` 搜索，排除依赖、构建和缓存；若目录被忽略，可在已限定的源码子目录使用 `--no-ignore`。找不到时说明未决证据，不扩展到其它盘符。
- 源码仓库保持只读：不联网拉取、不构建、不格式化、不修改。源码层需记录相对文件、行号及仓库版本，并与真实页面运行验证分开报告。
