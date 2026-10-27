# UI 生命周期与独立宿主

Windows 的审批/处理窗口使用用户明确启动的本地 UI host。MCP 只提交固定入口请求，不启动 host，不安装服务或计划任务；连接结束不会向 host、observer 或 UI 发送终止信号。审批、执行、SQL、执行锁和审批签名协议仍由原 UI/writer 处理。

## 启动与版本

当工具返回 `ui_host_required=true` 时，`ui_host_command` 是精确 argv 列表。用户从独立终端执行该命令，再重试原工具。命令使用固定 runtime package 中的 `scripts/ui_host.py --runtime-root <package>`；不应从 MCP 的子进程树启动。host 校验完整 manifest 和冻结 Python 身份，持有独立版本/解释器租约。UI 和 observer 分别持有自己的租约，MCP 退出不会释放它们。

生产 host 必须通过实际 `IsProcessInJob` 检查确认不在任何 Windows Job 内；不可读或仍在 Job 内时返回 `ui_host_isolation_unverified`，要求从独立终端启动。显式启动本身不构成已隔离的证据。首版不尝试放宽 SDK Job、不使用未验证的 breakaway、不自动切换版本。

host 在当前用户私有目录内轮询固定 schema 请求，校验随机 host nonce、startup ID、固定包目录、解释器、脚本、参数、日志和回执位置。仅接受 `approval_app.py` 的单事务/队列/计划报告及 `abort_session_app.py`；不提供任意进程、SQL 或审批入口。旧单事务的空 conversation scope 仍受现有业务校验。队列的 generation/token 仅通过两个固定环境字段传递。

## 生命周期证据

每次尝试有独立 startup ID，按 launch key 增加 generation；旧 attempt 不被新窗口覆盖。每个 attempt 保留：

- 最新 JSON 快照与 append-only `.events.jsonl` 关键事件，受同一 OS receipt 锁控制；序号单调递增，重复 heartbeat 只更新最新值。日志固定 schema，关键事件不含异常正文、设计、参数或凭据，单 attempt 事件预算 256 KiB。
- 固定 runtime 的版本、manifest 摘要、实例 ID 和 Python 身份摘要。
- launcher 与 actual UI 各自的 PID/创建 token，observer 身份、可见/审阅状态、phase、心跳、正常关闭标记和真实退出证据。

observer 由 host 启动，独立于 MCP；它在 UI claim 后持有真实 UI 进程 handle，即使 venv 转发导致 launcher 与 UI PID 不同也分别记录。Windows 用 `WaitForSingleObject` 判断是否退出，再取 `GetExitCodeProcess`，真实退出码 `259` 不被误认为仍在运行。launcher 的退出码永远不作为 UI 退出码。只能确认 launcher 已消失但 UI 未 claim、身份不可读或监测丢失时，UI 状态保持 `unknown`，不能据此重开或认定未执行。

`ready` 保持“窗口已可见且进入事件循环”语义，同步写当前 phase、`visible`、`review_ready` 和 `preview_pending`，避免上一条 loading 心跳被当成当前状态。关闭与异常是不同事件，迟到 ready/heartbeat/close 不能把终态失败改回成功。心跳停滞与进程退出分别报告。

## 无业务验收

`tests/test_ui_lifecycle.py` 与 `tests/fixtures/ui_lifecycle_sdk_stub.py` 使用本机已安装 venv 的实际 MCP SDK；只运行临时 heartbeat/sleep 夹具，不导入业务服务、不创建真实会话、不访问业务数据库。夹具 host 仅允许测试父进程预先指定的一个 stub，有固定生存上限并清理。测试父进程直接持有 SDK 真实 Job handle，核对各进程的 Job 成员关系。

覆盖旧直接启动路径在 SDK 超时清理时 UI 消失，以及独立 host 在持续连接、正常 EOF、服务端超时、客户端异常退出、嵌套 Job 和断线后 UI 自行异常退出时的行为。检查 host/observer/UI 均不属于 SDK teardown Job，venv PID 转发、正常退出码 0、异常退出码 7 与退出码 259。另测 unknown 身份、迟到事件、ready phase、请求限制和生产 Job 隔离拒绝。

这组复现证明当前 Windows/SDK 的同类机制与修复行为，不倒推历史现场的具体终止者。源码交付不启动生产 host、不重启 MCP、不安装插件。非 Windows 显式直接路径创建独立 session；Windows 的 `lifecycle_mode=direct` 仅用于有限隔离夹具，正式工具不传此参数。
