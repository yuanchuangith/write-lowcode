# GXP 低代码受控编辑

编辑已有未发布动作草稿：原生画布生成、独立复验、完整候选 C# 编译、逐实际 SQL 人工审批和保存后核对。支持同一插件选择全量、增量或自动模式；默认全量。同一草稿的元件、JS、C# 修改合并为一条主记录更新，只审批一次；额外实际 SQL 分别审批。

## 文档入口

| 要做什么 | 阅读位置 |
| --- | --- |
| 开发、调试、验证和交付 | [主要开发文档](DEVELOPMENT.md)，开发流程统一在此维护 |
| 启用编辑、定位草稿、规划和审批 | [编辑技能](skills/gxp-lowcode-edit/SKILL.md) |
| 查某类操作、原值守卫或参数路径 | [操作契约](skills/gxp-lowcode-edit/references/patch-schema.md)和[目标定位](skills/gxp-lowcode-edit/references/targeting-and-source.md) |
| 固定需求、场景和业务验收 | [业务复核](skills/gxp-lowcode-edit/references/business-review.md) |
| 处理审批状态、恢复、取消或多动作队列 | [会话流程](skills/gxp-lowcode-edit/references/session-workflows.md)，只读对应分支 |
| 选择全量、增量或自动生成 | [生成模式](docs/incremental-generation.md) |
| 查看或修改本机数据库设置 | [统一设置](skills/gxp-lowcode-edit/references/database-settings.md) |
| 查专题参考、优化交付及历史测量 | [资料索引](docs/README.md) |

当前版本以 [.codex-plugin/plugin.json](.codex-plugin/plugin.json) 为准（0.1.47）。源码开发和维护统一从 [DEVELOPMENT.md](DEVELOPMENT.md) 开始。

## 最短使用流程

1. 首次业务编辑明确调用 `$gxp-lowcode-edit`，提供已有动作 code / RefId、修改要求及需要保留的行为。同一已授权对话和草稿可继续，不重复启用。普通只读排查和“按方案继续”本身不激活编辑。
2. 定位节点，按需精读字段，从原始要求固定验收期望；一次规划同一草稿的已确认修改。Look 和 `handoff_ref` 都是可选证据来源。
3. 计划及复核通过后打开正式本机审批窗口，由用户核对实际 SQL 并确认执行。完成保存核对及会话释放后，再人工检查画布、发布并验证运行结果。

详细门禁以[技能入口](skills/gxp-lowcode-edit/SKILL.md)和[安全契约](skills/gxp-lowcode-edit/references/safety-contract.md)为准。

## 大配置按需读取

MCP 默认返回摘要；**全量生成是生成模式，摘要是响应格式**。`begin_edit_session` 和 `inspect_edit_context` 默认 `response_detail="summary"`；后者可用 `parameter_paths` 聚焦字段。显式 `include_context` 继续按旧语义优先处理。

优先使用服务端签名的 `guard_ref` 绑定基线旧值，避免把长配置再传回模型；它与 `expected_old_value` 二选一。需要精确内容时用 `read_edit_value`：预算按 UTF-8 字节计算，默认 16 KiB、最大 64 KiB，偏移按 Unicode 码点计算。依赖同批中间结果的旧值继续使用完整格式。细节见[目标定位](skills/gxp-lowcode-edit/references/targeting-and-source.md)和[操作契约](skills/gxp-lowcode-edit/references/patch-schema.md)。

`read_plan_review` 默认返回摘要，可按单项字段续读；完整配置和审批证据仍保留在服务端。只读取当前判断需要的字段，不自动遍历全部页。

## 数据库设置

在任一插件目录运行 `python scripts/database_settings.py`（macOS/Linux 可用 `python3`）打开 Look/Write 共用窗口。地址、端口、库名统一管理，各自账户和编译器等专属设置保留；密码隐藏输入到操作系统凭据库。用户也可明确指定非敏感字段，由 `database_settings` 测试后保存；这个本机入口无需启用编辑技能或创建会话。旧编辑流程未结束时阻止切换，测试失败保留原配置。

本地 MCP 共 29 个工具；`find_edit_nodes` 从会话备份中分页查找分组和节点，`inspect_edit_context` 默认返回参数摘要及绑定原值的 `guard_ref`，`read_edit_value` 按需分页读取精确值。`read_plan_review` 默认返回证据摘要，可继续读取指定字段；完整证据仍保留在计划和审批工件。`record_graph_review` 记录已核对、拒绝、待发布等复核进度，均不写业务数据。`database_settings` 只处理用户明确要求的本机设置，不授予业务编辑或审批权限。安装和普通排查不会自动改库，模型不接收密码或连接串。

源码维护不会自动安装或更新已运行插件、重启 Codex、保存或发布业务草稿。
