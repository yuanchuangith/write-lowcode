# Write 参考与历史资料索引

**开发主文档只有 [DEVELOPMENT.md](../DEVELOPMENT.md)**。架构、运行、镜像同步、验证及交付流程统一在主文档维护；本页仅索引专题和历史资料。日常使用从[仓库入口](../README.md)或[编辑技能](../skills/gxp-lowcode-edit/SKILL.md)开始。

## 专题规范

| 内容 | 文档 |
| --- | --- |
| 授权、正常编辑流程与按需阅读 | [SKILL.md](../skills/gxp-lowcode-edit/SKILL.md) |
| 定位、模板、参数摘要与精确值 | [targeting-and-source.md](../skills/gxp-lowcode-edit/references/targeting-and-source.md) |
| 操作字段、路径、数组选择器与守卫 | [patch-schema.md](../skills/gxp-lowcode-edit/references/patch-schema.md) |
| 需求归因、场景、断言和报告读取 | [business-review.md](../skills/gxp-lowcode-edit/references/business-review.md) |
| 编译器绑定、事件、JS / C# 检查 | [frontend-compiler-contract.md](../skills/gxp-lowcode-edit/references/frontend-compiler-contract.md) |
| 证据复用和按需补证 | [evidence-and-performance.md](../skills/gxp-lowcode-edit/references/evidence-and-performance.md) |
| 写后图谱核对及完整案例合并 | [post-write-graph-review.md](../skills/gxp-lowcode-edit/references/post-write-graph-review.md) |
| 整批修复、缓存边界与耗时 | [batch-repair-and-performance.md](../skills/gxp-lowcode-edit/references/batch-repair-and-performance.md) |
| 审批、状态、恢复、队列 | [session-workflows.md](../skills/gxp-lowcode-edit/references/session-workflows.md) |
| 账户、并发和签名边界 | [safety-contract.md](../skills/gxp-lowcode-edit/references/safety-contract.md) |
| 本机连接设置 | [database-settings.md](../skills/gxp-lowcode-edit/references/database-settings.md) |
| 生成模式及选择边界 | [incremental-generation.md](incremental-generation.md) |
| 架构、运行环境、镜像同步和离线测量 | [开发主文档](../DEVELOPMENT.md) |

响应默认摘要，生成默认全量；两者独立。工具精确签名以当前 MCP schema 为准，完整证据留在服务端。Write 可独立使用，不以 Look 诊断或导出交接包为前提。

## 最新优化交付与附件

[编辑工具故障修复实施计划](editor-incident-repair-plan-2026-10-27.md)覆盖基线诊断、版本资源、审批窗口生命周期及 hash 辅助恢复；当前为待实施方案，不代表已经安装或修复。

`create_conversation` 返回类型热修复见[2026-10-27 修复记录](conversation-response-hotfix-2026-10-27.md)。这是传输契约修复，不改变已冻结的优化测量。

2026-10-27 的来源扫描优化、有限排查兜底及30例独立路由回放见[本次交付报告](look-write-optimization-delivery-2026-10-27.md)，语料、冻结哈希与各次回放位于 `evaluation-2026-10-27/`。以下2026-10-26附件保持原样。

先读[Look / Write 优化实施与验收](look-write-optimization-delivery-2026-10-26.md)，再按问题读取附件。该报告描述本轮实施时的交付状态；后续安装和文档整理不追改冻结测量。

| 要核对什么 | 结果与原始数据 |
| --- | --- |
| 六类大配置的最终完整流程减量 | [最终响应测量](payload-reduction-final-2026-10-26.json)；[第一批结果](payload-reduction-results-2026-10-26.md)及[第一批数据](payload-reduction-benchmark-2026-10-26.json)为历史对照 |
| 固定业务语料、误绿、误阻和未验证 | [准确性记录](write-accuracy-implementation-results-2026-10-26.md)、[结果附件](business-accuracy-results-2026-10-26.json) |
| 同机冷 / 热服务成对基准 | [基线](service-pipeline-baseline-2026-10-26.json)、[成对结果](service-pipeline-pairs-2026-10-26.json) |
| 发现节点分页的分配量与一致性 | [基线](discovery-pages-baseline-2026-10-26.json)、[候选](discovery-pages-candidate-2026-10-26.json) |
| Node / C# 并行实验及未满足门槛 | [实验附件](validation-overlap-probe-2026-10-26.json)；生产服务保留串行 |
| 技能路由离线回放 | [说明](skill-routing-evaluation-2026-10-26.md)、[冻结语料](skill-routing-corpus-freeze-2026-10-26.json)、[回放结果](skill-routing-replay-2026-10-26.json) |

字节减少不等于模型端到端提速。最新报告明确列出样本、局部回退和未测范围；不能用历史微基准替代当前服务结论。

## 历史评估与实现记录

以下文件保留原路径，方便追踪当时的决定、复现命令和附件。旧方案、版本号及“尚未实现”状态按记录时间理解。

| 阶段 / 主题 | 文档 |
| --- | --- |
| 优化前评估与批次建议 | [速度与准确性优化报告](speed-and-accuracy-optimization-report-2026-10-26.md)，[模式探针](speed-accuracy-mode-probe-2026-10-26.json)、[Look 探针](speed-accuracy-look-probe-2026-10-26.json) |
| 编辑技能功能取舍 | [skill-improvement-review.md](skill-improvement-review.md) |
| 离线流程验证 | [skill-workflow-evaluation.md](skill-workflow-evaluation.md) |
| 路由语料原稿 | [skill-routing-corpus-2026-10-26.json](skill-routing-corpus-2026-10-26.json)；冻结回放以上节附件为准 |
| 生成质量与速度 | [generation-quality-and-speed.md](generation-quality-and-speed.md) |
| 交接、批次修复与编译准备 | [diagnosis-edit-performance.md](diagnosis-edit-performance.md)；当前调用细节看技能参考 |
| 2026-10-22 编译准备实测 | [报告](optimization-performance-report.md)、[附件](optimization-benchmark-2026-10-22.json) |
| 2026-10-22 优先优化实测 | [报告](priority-optimization-measurements.md)、[附件](optimization-priority-benchmark-2026-10-22.json) |
