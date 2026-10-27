# Write 开发文档

本文是 Write 唯一持续维护的开发主文档，覆盖架构、改动入口、本地调试、验证和交付。面向用户的入口在 [README.md](README.md)，模型执行规则在 [SKILL.md](skills/gxp-lowcode-edit/SKILL.md)。历史报告与测量附件通过 [docs/README.md](docs/README.md) 查阅，不另起平行的开发指南。

## 架构与代码入口

服务处理已有未发布 ActionDesign 草稿：注册对话 → 建立独占会话及不可变备份 → 定位与读取 → 固定需求并规划 → 原生生成和独立复验 → 业务复核 → 人工 SQL 审批 → 保存核对及释放。Look 是可选的只读证据来源；不需要先完成诊断或导出交接包。

| 改动内容 | 主要入口 |
| --- | --- |
| MCP 工具、参数及默认值 | [mcp/server.py](mcp/server.py) |
| 工作流与编排 | [service.py](mcp/gxp_editor/service.py)、[workflow_guidance.py](mcp/gxp_editor/workflow_guidance.py) |
| 会话、基线及并发 | [state.py](mcp/gxp_editor/state.py)、[backup.py](mcp/gxp_editor/backup.py)、[repository.py](mcp/gxp_editor/repository.py) |
| 节点发现、上下文、值传输与守卫 | [node_discovery.py](mcp/gxp_editor/node_discovery.py)、[edit_context.py](mcp/gxp_editor/edit_context.py)、[value_transport.py](mcp/gxp_editor/value_transport.py)、[edit_guards.py](mcp/gxp_editor/edit_guards.py) |
| 操作校验及原生生成 | [operation_preflight.py](mcp/gxp_editor/operation_preflight.py)、[compiler.py](mcp/gxp_editor/compiler.py)、[native_compiler/](mcp/gxp_editor/native_compiler/bridge_core.js) |
| 场景、断言及业务复核 | [logic_review.py](mcp/gxp_editor/logic_review.py)、[logic_scenarios.py](mcp/gxp_editor/logic_scenarios.py)、[canvas_assertions.py](mcp/gxp_editor/canvas_assertions.py)、[review_response.py](mcp/gxp_editor/review_response.py) |
| C# 完整编译 | [runtime_validation.py](mcp/gxp_editor/runtime_validation.py)、[runtime_workspace.py](mcp/gxp_editor/runtime_workspace.py) |
| 审批、执行、保存后核对与恢复 | [approval.py](mcp/gxp_editor/approval.py)、[writer.py](mcp/gxp_editor/writer.py)、[post_write.py](mcp/gxp_editor/post_write.py)、[recovery.py](mcp/gxp_editor/recovery.py) |
| 技能及客户端声明 | `skills/`、`.claude/skills/`、[插件 manifest](.codex-plugin/plugin.json)、[MCP 配置](.mcp.json) |

## 修改时必须保留的契约

| 主题 | 规则与详细规范 |
| --- | --- |
| 授权与范围 | 首次业务编辑显式启用，同一已授权对话和草稿继续复用；设置入口不授予编辑权限。见[技能入口](skills/gxp-lowcode-edit/SKILL.md)及[安全契约](skills/gxp-lowcode-edit/references/safety-contract.md)。 |
| 定位与旧值 | 目标来自会话备份；`guard_ref` 与完整旧值二选一，签名只代表不可变基线，批次中间值仍用完整格式。见[目标读取](skills/gxp-lowcode-edit/references/targeting-and-source.md)和[操作契约](skills/gxp-lowcode-edit/references/patch-schema.md)。 |
| 响应体积 | MCP 默认摘要，显式完整模式保留；显式 `include_context` 优先按旧语义。值分片偏移为 Unicode 码点，预算为 UTF-8 字节，默认 16 KiB、上限 64 KiB。 |
| 生成与编译 | 默认全量生成，与响应摘要独立；独立复验及完整候选 C# 编译保留。见[生成模式](docs/incremental-generation.md)和[编译器契约](skills/gxp-lowcode-edit/references/frontend-compiler-contract.md)。 |
| 业务准确性 | 先从原始需求固定期望，不从候选反推或放宽。`passed` 需要完整唯一断言与观察值核对，`failed` 和 `unverified` 分开。见[业务复核及报告读取](skills/gxp-lowcode-edit/references/business-review.md)。 |
| 审批与恢复 | 人工审批实际 SQL 及参数；验证并释放才完成保存，不代表发布或运行通过。见[会话流程](skills/gxp-lowcode-edit/references/session-workflows.md)。 |
| 证据与图谱 | 先复用有效证据，缺口按需补查。写后图谱修改需单独同意，完整案例及 CAS 合并；草稿不证明发布。见[证据复用](skills/gxp-lowcode-edit/references/evidence-and-performance.md)和[写后图谱复核](skills/gxp-lowcode-edit/references/post-write-graph-review.md)。 |

精确工具签名以当前 MCP schema 为准；修改外部参数时同步检查服务、验证器、技能参考及调用方，不能只改说明。

## 本地运行与客户端入口

仓库提供 Windows 启动脚本。在仓库根目录执行：

```powershell
.\scripts\setup.ps1
.\scripts\start_mcp.ps1
```

`setup.ps1` 在 `%LOCALAPPDATA%/GxpLowcodeEditor/.venv` 创建环境并安装 [requirements.txt](requirements.txt)；启动器用该环境运行 stdio 服务。后面的 Python 命令应使用已具备依赖的环境。编译相关开发还需要 Node、真实前端 compiler bridge，以及用于非空 C# 候选的 .NET SDK 和只读引用；就绪约束以编译器契约为准，不猜测或修改外部仓库。

[.mcp.json](.mcp.json) 声明本地启动器，[.codex-plugin/plugin.json](.codex-plugin/plugin.json) 声明 Codex 插件。仓库没有一键 Codex / Claude 安装器；准备运行环境、同步技能与更新客户端安装是三个不同步骤。源码改动不会自动更新 Codex 缓存或重启客户端。

数据库设置仅按用户明确要求通过 [database_settings.py](scripts/database_settings.py) 或本机工具处理；具体见[统一设置](skills/gxp-lowcode-edit/references/database-settings.md)。普通开发和离线验证不读取凭据、不访问业务库、不保存或发布草稿。

## 开发与验证流程

1. 先查看工作区及相关契约，明确改动涉及工具、服务、技能还是测量。保留已有用户修改。
2. 修改实际行为及其必要说明；以固定期望、来源漂移、失败阻断等可观察结果验证，避免只匹配实现或措辞的测试。
3. 只运行受影响的定向回归；失败或新证据才扩大范围。文档改动校验链接及必要的技能契约即可，不触发完整编译或业务流程。
4. 若改变工具契约或可安装行为，同步 manifest 版本、技能参考和测量限制；历史报告及原始附件保持冻结。
5. 审阅差异后本地提交。客户端安装更新按具体请求执行，和源码交付分别记录。

文档或技能改动的最小检查：

```powershell
python scripts/sync_skills.py --check
python -m unittest discover -s tests -p test_skill_contract.py -v
git diff --check
```

代码改动按以下范围选择测试模块，以 `python -m unittest tests.<模块名> -v` 运行；需要原生环境的测试按自身前置条件执行，跳过不等于通过。

| 修改范围 | 相关测试模块 |
| --- | --- |
| 上下文、签名与响应分片 | `test_payload_reduction`、`test_context_service`、`test_operation_preflight` |
| 节点发现与完整性 | `test_node_discovery`、`test_edit_context` |
| 需求、场景与报告 | `test_business_review_service`、`test_business_accuracy_corpus`、`test_scenario_oracle`、`test_review_response` |
| 生成、绑定与就绪复用 | `test_codegen_consistency`、`test_generation_modes`、`test_service_optimization`、`test_service_readiness` |
| 执行与恢复 | `test_writer`、`test_transaction`、`test_session_release`、`test_recovery_service` |

## 技能镜像维护

只编辑 `skills/`；`.claude/skills/` 是受跟踪镜像。编辑后执行：

```powershell
python scripts/sync_skills.py
python scripts/sync_skills.py --check
```

同步器只处理 Git 跟踪的源文件。新增源文件先暂存，再同步并暂存对应镜像后检查。联接或硬链接已指向源文件时不重写；不会删除文件或覆盖冲突的未跟踪用户文件。删除源文件时，单独核对并移除过期镜像。检查模式比较受跟踪文件集合及逐字节内容。

## 离线测量与准确性评估

下列命令使用本地或合成输入，不执行业务 SQL、草稿保存或发布；输出写入忽略跟踪的 `output/`。保存工件和基线可能含完整设计证据，不粘贴到性能日志或模型响应。

```powershell
python scripts/benchmark_payload_reduction.py --output output/payload.json
python scripts/profile_generation.py --before before.json --after after.json --output output/profile.json
python scripts/profile_generation.py --artifact saved-approval.json --baseline baseline.json --output output/profile.json
```

`profile_generation.py` 接收原始 `{actionData, globalVariables}` 或含 `data` / `data_json` 的设计。含 `js_code` / `csharp_code` 时比较代码对齐。工件模式可用 `--state-db path/to/editor.sqlite3` 只读查基线，Windows 默认现有 `%LOCALAPPDATA%/GxpLowcodeEditor/state/editor.sqlite3`；不初始化或迁移该库。为复现方便，优先显式提供脱敏基线文件。

显式 `--frontend path/to/frontend` 才启用真实原生编译；`--bridge` 默认该仓库下的 `dist/action-design-compiler/bridge.js`，原始画布还需 `--action-code`。原生探针在内存做同参数修改，需含参数化的 SelectData、QueryData、SetVariable 或 SetVariableValue 节点；每个 Node 进程超时 25 秒。`--repeats` 为 1–3 次模型执行，默认 3 次，包含独立分析采样；不执行生成的业务代码。

```powershell
python scripts/benchmark_edit_pipeline.py --frontend path/to/frontend --output output/pipeline.json
python scripts/evaluate_business_accuracy.py --frontend path/to/frontend --native --output output/accuracy.json
python scripts/benchmark_service_pairs.py --baseline path/to/baseline-worktree --frontend path/to/frontend --rounds 5 --sizes 6 1000 --output output/service-pairs.json
```

`benchmark_edit_pipeline.py` 使用六组合成画布，覆盖冷 / 热生成、独立复验及错批修复；可选 `--csharp` 只用 net9.0 合成类和零应用引用，不代表生产兼容或审批证据。真实服务中独立 Node 和 C# 校验仍串行；实验门槛未全部满足时不合入并行。

测量须固定基线、同机交替、每场景至少五轮，冷 / 热分开，核对完整候选及审批绑定哈希。报告中位数和范围；误绿、误阻与未验证分别统计。`timings` 的嵌套阶段不可直接相加，字节减少不能推出模型端到端提速。日志、缓存和计时边界见[批次修复与性能](skills/gxp-lowcode-edit/references/batch-repair-and-performance.md)。

现有结果以[优化交付报告](docs/look-write-optimization-delivery-2026-10-26.md)及其冻结附件为准；报告列出的未测范围和回退样本必须随结论保留。

## 文档维护约定

- 开发架构、命令、验证和交付流程只在本文维护；`README.md` 保留产品入口，`scripts/README.md` 仅导航到本文。
- 技能的授权、操作和证据规范维护在 `SKILL.md` 与 `references/`，本文只保留必要摘要和链接，避免出现两套规则。
- `docs/README.md` 负责专题及历史资料导航。历史评估、计划和 JSON 附件保留原路径，不用新结论覆盖旧测量，也不继续作为主开发流程更新。
