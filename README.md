# GXP 低代码受控编辑

编辑已有未发布动作草稿：原生画布生成、独立复验、完整候选 C# 编译、逐实际 SQL 人工审批和保存后核对。支持同一插件选择全量、增量或自动模式；默认全量。同一草稿的元件、JS、C# 修改合并为一条主记录更新，只审批一次；额外实际 SQL 分别审批。

- [编辑技能](skills/gxp-lowcode-edit/SKILL.md)：显式启用、定位、规划、审批及恢复流程。
- [排查交接与性能优化](docs/diagnosis-edit-performance.md)：只读证据交接、操作模板、批次错误、分段计时及编译准备缓存。
- [生成模式](docs/incremental-generation.md)：全量／增量／自动选择边界。
- [数据库统一设置](skills/gxp-lowcode-edit/references/database-settings.md)：在任一插件目录运行 `python scripts/database_settings.py`（macOS/Linux 可用 `python3`）打开 Look/Write 共用窗口；地址、端口、库名统一管理，各自账户和编译器等专属设置保留。密码隐藏输入到操作系统凭据库。也可明确告诉模型要改的非敏感字段，由 `database_settings` 测试后保存；此本机入口无需启用编辑技能或创建会话。旧编辑流程未结束时阻止切换，测试失败保留原配置。

本地 MCP 共 26 个工具，其中 `database_settings` 只处理用户明确要求的本机设置，不授予业务编辑或审批权限。安装和普通排查不会自动改库，模型不接收密码或连接串。

源码维护不会自动安装或更新已运行插件、重启 Codex、保存或发布业务草稿。
