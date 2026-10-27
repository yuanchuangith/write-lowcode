# create_conversation 返回契约修复（2026-10-27）

用户实际调用 `create_conversation` 时，FastMCP 在工具执行后拒绝返回：`timings` 是列表，`total_elapsed_ms` 是浮点数，但公开函数声明为 `dict[str, str]`。计时包装器保留原函数注解，SDK 因而生成要求所有值均为字符串的 `DictModel`；失败响应中的布尔和字典等字段同样受影响。

公开返回注解改为 `dict[str, Any]`，与其他工具一致。内部服务和状态库的纯字符串返回类型保持精确，返回字段及业务行为不变。该注解与计时包装器的组合早于本次优化；此前只验证启动和工具列表，遗漏了实际调用后的输出转换。

新增回归通过实际 `mcp.call_tool` 验证合法标签的成功响应及超长标签的失败响应，覆盖计时、错误和补证字段。使用临时 SQLite 状态库，成功时只新增一个本地对话标记，编辑会话及队列均为零；失败时不新增对话、会话或队列。

修复前两条新回归均在 SDK 输出转换处复现失败；修复后以下21项定向回归全部通过：

```powershell
$env:PYTHONPATH='mcp;.'
& "$env:LOCALAPPDATA/GxpLowcodeEditor/.venv/Scripts/python.exe" -m unittest tests.test_conversation_tools tests.test_performance_trace tests.test_evidence_guidance -q
```

已按本地 marketplace 安装流程更新插件，并通过已安装缓存中的实际 `scripts/start_mcp.ps1` 启动独立 stdio 进程验证。以 `GXP_LOWCODE_EDITOR_STATE` 指向新临时库、配置路径指向不存在的临时文件；工具列表为29项，合法与超长标签响应均正常通过协议传输。两次调用后临时库计数为 `conversations=1`、`sessions=0`、`edit_queues=0`，整个验证无需业务配置。

返回校验错误发生在本地标记插入之后，历史失败调用可能留下未使用的对话标记；创建对话本身不建立草稿编辑会话或持续占用草稿锁。本次未读取、删除或释放用户的实际会话，也未访问业务库或修改草稿。

插件版本为 `0.1.49+codex.local-20261027-response-contract`。本地安装后须重连 MCP 或开启新会话，已有进程不会自动重新加载代码。本记录不替代或追改此前冻结的性能、准确性评估。
