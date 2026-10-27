# 编辑工具故障修复交付记录（2026-10-27）

源码实施完成，版本为 `0.1.50+codex.local-20261027-incident-recovery`。本次只在 Write 仓库开发、隔离验证和本地提交；未安装或重装插件，未修改已安装缓存，未重启现有 MCP，未启动生产 UI 宿主，未读取凭据或操作实际会话、草稿及业务数据库。历史事故报告、原始回执和原实施计划保持原样。

## 实现与兼容

| 批次 | 实现 | 本地提交 |
| --- | --- | --- |
| A | 统一只读比较器，区分正文、仅时间、关系、目标状态和证据未知；诊断摘要不返回正文、不改变原 hash/SQL CAS/HMAC 算法 | `6b556ae` |
| B | 完整字节 manifest、暂存后验证及原子运行快照；独立进程/解释器租约；共享 venv 更新拒绝活动及无法核实的使用者；Node/Python 身份预检 | `32dfb81` |
| C/D | 每 startup 独立事件和状态，分开 launcher/实际 UI 身份及退出码；采用用户明确启动的外部 host 与独立 observer；不使用未经证明的 breakaway | `a169c62` |
| E | 接手诊断、只读重开预检、队列 revision/generation/token 交接、认证主体与目标 scope 签名绑定、写前来源重验、恢复归因持久化 | `1031850` |
| 文档及构建 | 开发文档、MCP 工具说明、恢复技能参考及镜像、独立测量与本报告，最终版本及运行 manifest | 随本报告提交 |

发现、目标读取、引用修复、规划及进入审批均复用比较器。草稿和完整关系集合在显式 REPEATABLE READ 一致只读快照内获取；溢出、超时、缺状态或读取失败为 unknown。仅修改时间变化仍保留原 CAS 阻断。接手已成功时，后续诊断失败不回滚或掩盖转交，返回 `attached=true` 和 `baseline_status=unknown`。

重开从原合法工件取核心旧指纹，完整旧 baseline 损坏不成为新的硬前提。预检重新验证草稿/关系、账户模式/grants、原签名认证主体和 scope、当前 compiler binding、要求与当前归属/指针；进入启动交接前重核工件指纹。旧合法工件不补签新字段，保留 `legacy_grants_only`；恢复事务不被强加普通 write 的业务复核字段。未知、漂移或执行标记仍阻断重开执行。

队列保留启动代次时固定 revision，实际退出回执必须对应相同 generation 和 token 摘要。子窗口取得自身锁后再次检查，成员变化拒绝旧预检；短控制事务非阻塞失败，不跨业务查询或用户等待。执行 claim 继续检查队列成员归属、当前事务及状态。启动不明时保留 reservation；活着但卡住的队列不自动终止。完成项不重新执行，结果不明项先核对。

新工件把实际连接的 `CURRENT_USER()` 与数据库 scope 放入原 `database_safety` 容器，按原事务签名算法冻结。writer 在实际写连接再次核对主体，write 在 claim 前复核当前编译来源；旧内容/关系、目标状态、SQL 影响行数、执行锁、人工审批和提交后复核继续保留。

`execution_outcome`、`commit_evidence`、`verification_scope` 和当前快照匹配结果持久化到本地，贯通 recovery、status、verify、finish 和历史查询。当前结果匹配但提交归因未知时，仍可核验快照，不能推断为已确认提交，也不能重试原事务。历史回执明确没有重读当前草稿；`executed=false` 且 outcome unknown 不表示从未执行。

## 验证结果

环境：本机 Windows、现有 `%LOCALAPPDATA%/GxpLowcodeEditor/.venv` Python 与实际 MCP SDK。只使用合成 ID、临时 SQLite/文件和有固定生存期限的 stub；无业务 SQL 执行。

| 验证组 | 结果 | 范围 |
| --- | --- | --- |
| 最终核心组合 | 357 项通过，21.187 秒 | 比较器、服务、状态、主体/scope、writer、恢复归因、队列并发、旧工件/恢复兼容、MCP 输出转换、技能契约 |
| 最终运行资源组合 | 89 项运行，86 通过、3 跳过，17.291 秒 | 快照原子性与并发、延迟导入、资源删除/篡改、两个版本、租约/共享解释器协调、Node 来源、启动脚本 |
| Windows UI/实际 SDK 组合 | 36 项通过，57.137 秒；随后新增锁释放回归 1 项通过 | 真实 Job 成员关系、正常 EOF、服务端超时、异常断线、嵌套 Job、实际 UI 与 launcher PID 不同、断线后退出码留存、unknown/迟到事件/ready 语义 |
| 构建及文档 | 通过 | 最终 manifest 全量字节核验、技能镜像/契约、`git diff --check` |

运行资源组跳过的是 `test_native_sources` 中需要 `GXP_TEST_COMPILER_BRIDGE` 的 3 个真实前端编译案例，不能计作通过。实际 MySQL 一致快照、行锁/CAS及生产草稿、真实业务运行未测试；数据库协议用受控夹具验证。现有编译器、真实引用与业务验收门禁仍保留。

可复跑核心新增覆盖：`test_incident_reopen`、`test_snapshot_read`、`test_queue_protocol`、`test_execution_evidence`、`test_principal_binding`、`test_baseline_comparison`。运行与生命周期覆盖：`test_runtime_package`、`test_ui_lifecycle`、`test_ui_launch`、`test_ui_recovery`。测试命令使用 `PYTHONPATH=mcp;.` 和现有依赖环境；完整 SDK 测试自带临时进程清理及生存上限。

实测旧直接 Popen 路径的 UI 属于 SDK teardown Job，并在 SDK 清理时退出。breakaway 创建调用成功时，探测子进程仍可能属于 SDK Job；嵌套场景也出现拒绝，故不以调用成功作为隔离证明。新宿主夹具在上述断线场景下，host/observer/实际 UI 均不属于真实 SDK Job，断线后仍可观测 UI 自行正常退出 0 或异常退出 7；额外覆盖真实退出码 259。生产 host 比夹具更严格：必须证明不在任何 Windows Job 内，否则拒绝启动。详见 [UI 生命周期契约](ui-lifecycle-contract.md)。这些实验不证明历史事故由某一具体进程终止，原事故该项仍只有高可信线索。

## 测量、构建与启用边界

[预检测量附件](editor-incident-preflight-measurements-2026-10-27.json)由 `scripts/measure_incident_preflight.py` 生成，四个场景交替各 5 轮：匹配、正文变化、读取不可用、编译来源变化。匹配及来源变化各调用草稿/关系/grants/compiler 入口一次；正文变化在比较后停止；数据库不可用不继续补用旧证据。中位耗时约 0.13–0.36 ms，仅为 Mock 编排开销，不是 SQL 数量、实际数据库往返、生产延迟或提速证明。没有覆盖历史测量文件。

最终 manifest 摘要为 `54070d8e301085ae6e569498f3b84cf54a822721cf4310900d0382271b537d71`，按本工作区最终实际字节生成并全量校验。打包或 checkout 转换换行后，须在最终分发字节上执行 `python scripts/build_runtime_manifest.py` 重建；不能混用某次构建的 manifest 和另一次构建的文件。运行快照不会自动清理，也不会在运行中切换版本；宿主在启动前删除缓存入口仍需完整入口或重新连接，源码无法控制宿主的缓存保留策略。

Windows 正式 UI 现在依赖显式外部宿主，未启动时返回 `ui_host_required` 和 argv 命令，不把窗口启动请求报告成成功。后续安装、宿主启动及实际 MCP 重连需用户另行明确要求；本次没有执行这些部署动作。常驻服务或计划任务也未配置。正常运行的 UI/observer 各持自己的运行版本与解释器租约，不随 MCP 结束释放。

当前 hash 只证明当前快照；正文及时间戳均被外部恢复的 ABA 历史仍需平台单调 revision/审计证明。没有真实生产样本时，本交付不宣称线上业务准确率、实际事故终止者或端到端生产行为已经验证。
