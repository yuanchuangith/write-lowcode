# 目标元件定位与源码仓库

## 怎样确定要修改的元件

源码仓库说明“元件怎样编辑和生成代码”；当前 ActionDesign 说明“哪个动作的哪个元件需要改”。不能只根据元件名称、字段名称或仓库搜索结果决定修改位置。

1. 从用户问题或已确认诊断锁定业务规则、动作 code/RefId、页面以及字段。优先复用本对话已有的当前草稿定位证据，包含草稿设计 ID、内容指纹、分组/节点 Key 和完整目标参数。没有这些证据时，通过只读 MCP 的 `get_design_versions` 确认当前发布和草稿；运行问题以发布副本取证，修改目标只能是当前草稿，发布副本不能代替草稿定位证据。
2. 缺少目标参数时，使用 `inspect_action` 读取相关分组和节点，缩小到一个节点或最多 10 个节点后读取完整 `paramsValue`。记录动作名称/code、RefId、设计 ID、分组名称/Key、画布行/索引、节点名称/Key、`elementKey`、上下相邻可见元件和实际参数。已有匹配证据时不重复查询；不得凭中文同名元件默认选第一个。
3. 沿字段的读取、条件和赋值找到故障点；涉及嵌套或分支归属且现有证据尚未确认时，使用只读 MCP 的 `inspect_control_flow` 核对父条件和闭合块。未经本次会话备份核对的旧诊断仅作线索，不能代替当前副本证据。
4. 开始编辑会话后，必须将复用证据中的草稿设计 ID、内容指纹和目标参数与本次会话备份核对；全部匹配才可直接构造操作。若有变化或缺项，重新读取相关位置，不沿用旧行号或旧参数。复用定位证据不代替会话入口、备份、并发、编译或审批前的实时检查。
5. 保留现有节点时，用当前 `node_key` 作为 `target_node_key`；插入时用已核对的相邻节点作为 `anchor_node_key`。`action_group_key` 来自当前草稿，新增元件的 `element_key` 来自真实注册表，不能臆造 Key 或参数结构。
6. 给用户展示可见位置、修改前后规则以及实际操作方式。若采用原元件中的自定义表达式，明确表达式所在配置字段；不能描述为“新增 IF 元件”。编译成功证明结构和生成规则可接受，不能证明业务定位正确。

只读 MCP 不可用且现有证据及会话备份都无法提供当前目标参数时，保留已确认结论、说明缺少的证据，不根据源码猜测业务节点。互不依赖的页面、元件契约和源码读取可并行；同一草稿的已确认操作汇总为一次计划。此取证流程不会自动从 debug 技能进入编辑；仍遵守编辑技能的用户授权和审批流程。

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
