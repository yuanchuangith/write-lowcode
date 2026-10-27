# Write 源码绑定检查加速验证（2026-10-27）

本次候选优化在离线服务链路中通过性能门槛，完整公开输出、候选产物、计划和审批证据保持一致。该结论适用于本报告的合成样例，不等同于真实业务准确率或线上端到端耗时结论。

## 版本与修改范围

- 基线：`c475db578d283b7c9e78487154fed1346a223fd9`。
- 基线副本：`output/write-speed-baseline-c475db5`；候选副本：`output/write-speed-candidate-c475db5`。
- 两个副本来自同一份基线归档，候选仅替换 `mcp/gxp_editor/compiler.py`。没有把同期的诊断指导或技能文档修改混入性能对比。
- 候选 `compiler.py` 与工作区文件 SHA256 相同：`62a332163a70397f6602af7800d6ea90d2e84b2213568307c55fb914c1aa88b4`。
- 基线 `compiler.py` SHA256：`96a10052a74dc02bcae0e020be276f3417fc9a4ea049c26728a2282754cf1151`。

优化把 Git 根目录、HEAD 和分支的三个进程合为一个进程，保留原样 porcelain 状态及 binary diff 的字节哈希。新请求先读取 Git 和 Node 身份，再扫描全部源码内容；有效的共享注册表命中只去掉随后的重复扫描。冷加载、损坏缓存重建及真实 Node 命令前后的内容检查仍保留。只有注册表被接受后才保存本次 Git 证据，已有实例发现绑定变化仍会拒绝继续使用。

没有引入跨请求的文件时间戳缓存、业务结果缓存、独立编译跳过或人工审批跳过。

## 基准方法

使用已安装 Write 虚拟环境的 Python，运行以下命令：

```powershell
$taskPython = Join-Path $env:LOCALAPPDATA 'GxpLowcodeEditor\.venv\Scripts\python.exe'
& $taskPython -B scripts/benchmark_service_pairs.py `
  --baseline output/write-speed-baseline-c475db5 `
  --candidate output/write-speed-candidate-c475db5 `
  --frontend G:/hoyi/updateComponents/gxp2.components `
  --rounds 5 --sizes 6 1000 `
  --output docs/write-source-binding-pairs-2026-10-27.json
```

共 5 轮配对，同一机器上交替执行整轮 baseline/candidate 子进程。每个实现进程分别执行 6 节点和 1000 节点样例的冷、热链路，每条链路依次调用：开始会话、首尾两页节点发现、检查节点上下文、制定修改计划、准备写入。共记录 240 个工具请求，每项对比有 5 个完整链路样本。

冷链路仅在开始会话前清空注册表和合同缓存，后续五个请求使用已填充缓存；热链路直接从已填充缓存开始。每个进程内顺序为冷→热，未重置操作系统文件缓存。业务数据库和仓库为合成实现，本地临时 SQLite、备份文件和前端 Node 编译器实际运行；服务链路样例生成的 C# 为空，审批窗口启动被替身替代。

## 性能结果

完整六请求链路的中位数及最小—最大值，单位为秒：

| 节点数 | 初始缓存 | 基线中位数（范围） | 候选中位数（范围） | 中位数变化 |
| --- | --- | --- | --- | --- |
| 6 | 冷 | 13.340（12.977—15.207） | 11.338（11.256—11.689） | -15.009% |
| 6 | 热 | 12.059（11.605—12.847） | 9.676（9.603—10.161） | -19.766% |
| 1000 | 冷 | 15.073（14.898—15.524） | 13.046（13.019—13.693） | -13.447% |
| 1000 | 热 | 13.592（13.313—14.151） | 11.534（11.491—12.579） | -15.138% |

四项门槛均通过：冷链路中位数退化不超过 5%，热链路中位数下降。原始数据中的 `performance_acceptance.accepted` 为 `true`。

两个节点规模的进程与扫描次数相同：

| 完整链路 | 基线全内容扫描→候选 | 基线 Git 进程→候选 | 基线 Node 进程→候选 |
| --- | --- | --- | --- |
| 冷 | 24→19 | 30→18 | 11→11 |
| 热 | 22→16 | 30→18 | 10→10 |

Node 次数包括版本探测，实际编译命令没有减少。单项工具的完整中位数、范围、分阶段计时和计数保存在原始 JSON 中。

## 一致性与回归验证

基准固定合成 Python UUID 和事务时间。每个完整公开响应仅排除 `trace_id`、`timings`、`total_elapsed_ms` 三个遥测字段后计算 SHA256，没有删除候选内容、SQL 或审批证据。全部 240 个响应在两实现、五轮及冷热缓存之间按同一工具和页码比较一致。

另行比较生成画布、JS、C#、关系产物的哈希，以及完整计划 artifact、实际计划 binding、完整审批 artifact、实际审批 binding 的 SHA256；绑定还通过生产函数独立重算核验。原始数据的以下字段均为 `true`：

- `candidate_hashes_match`
- `complete_public_outputs_match`
- `complete_plan_and_approval_evidence_match`

测试结果：

```text
python -m unittest tests.test_rule_cache tests.test_native_sources tests.test_git_provenance tests.test_compiler tests.test_service_readiness -q
Ran 50 tests in 3.156s — OK (skipped=3)

# 基准结束后设置 GXP_TEST_COMPILER_BRIDGE / GXP_TEST_FRONTEND_REPO，单独执行原先跳过的真实编译器测试：
python -m unittest tests.test_native_sources.NativeElementCoverageTests -q
Ran 3 tests in 14.607s — OK
```

新增回归覆盖：真实临时 Git 仓库的干净、脏工作区、暂存、二进制、重命名、未跟踪、detached HEAD 和 linked worktree 与旧五命令证据一致；畸形 Git 身份被拒绝；Git/Node 探测期间发生的同大小、同 mtime 源码修改仍被拒绝；失败重试不会沿用旧证据；已有实例不能切换到另一个缓存运行时。原生覆盖测试另外核对完整元素注册表，以及锁和其他元素的生成与结构拒绝行为。

## 证据边界

- 原始测量：[write-source-binding-pairs-2026-10-27.json](write-source-binding-pairs-2026-10-27.json)。
- 没有读取凭据、连接业务数据库、执行生成的业务代码、安装候选版本或修改外部前端仓库。
- 未测量模型决策准确率、模型耗时/输入 token、真实数据库/网络、真实审批界面或业务 C# 兼容性。
- 五个样本不支持 p95 结论，后台机器负载没有严格控制。
- 文件内容校验不是原子的仓库快照，不能声称消除并发修改的 TOCTOU 风险；本次保留了真实 Node 边界及新请求的源码重新校验。
