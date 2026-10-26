# MCP 响应体积优化验证

2026-10-26，以六组合成场景比较显式 full 兼容读取和默认 summary 读取。两条流程各调用相同的七个 MCP 工具：进入会话、查找节点、读取上下文、读取需要的精确值、生成计划、读取指定复核页、准备人工审批。

下表按实际 FastMCP 返回的文本块与 structuredContent 一并序列化为紧凑 UTF-8 JSON 计数，包含 trace/timings；不包含 SDK 网络帧。原始分阶段结果见 [JSON](payload-reduction-benchmark-2026-10-26.json)。

| 合成场景 | full 字节 | summary 字节 | 减少 |
| --- | ---: | ---: | ---: |
| 长中文值 | 1,362,815 | 50,481 | 96.296% |
| 嵌套过滤配置 | 710,284 | 50,477 | 92.893% |
| 原生 ID 数组行 | 3,290,746 | 81,799 | 97.514% |
| 大父级参数 | 878,944 | 50,985 | 94.199% |
| 大原生契约 | 426,799 | 50,475 | 88.174% |
| 大复核前后值 | 1,218,813 | 50,485 | 95.858% |

每组均校验两条流程的候选哈希、绑定计划哈希、审批候选哈希和完整业务复核哈希相同。完整旧值在进入严格预检及编译器前恢复；工件保留完整操作、原值和复核证据，审批仍无任何同意记录。分页测试覆盖中文、emoji、完整 JSON 重组、预算、值指纹、旧值引用篡改、关系变化和分页期间会话归属变化。`offset` 按 Unicode 码点计数，UTF-8 字节只用于响应预算。

此验证使用真实服务、状态存储、签名备份、预检、复核和审批工件；仓库、编译器、运行时与 UI 使用明确的模拟实现，无业务数据库访问或写入，也不执行业务代码。结果证明这些样例的响应字节减少，不代表真实编译或模型推理耗时的改善。`read_edit_value.max_output_bytes` 限制服务值响应及其 guard/binding，外层 MCP 包装和 telemetry 额外计入本次完整流程统计。

复现：

```powershell
python scripts/benchmark_payload_reduction.py --output docs/payload-reduction-benchmark-2026-10-26.json
python -m unittest tests.test_payload_reduction tests.test_context_service tests.test_review_response tests.test_conversation_tools tests.test_edit_context tests.test_session_retry tests.test_handoff_import tests.test_review_display_context -q
```

联合测试通过 86 项；追加 MCP 参数转发和技能验证后，payload/tools/skill 定向测试通过 35 项。技能验证器与镜像一致性检查通过。计时及随机 trace 会让复测字节数轻微变化。
