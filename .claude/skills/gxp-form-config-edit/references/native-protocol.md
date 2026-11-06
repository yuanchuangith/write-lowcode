# 原生协议及元数据依据

开发设计器取证已确认 `GET /inbiz/api/services/engines/v3/page/loadDetail` 读取 `Component` JSON 字符串，`PUT /inbiz/api/services/engines/v3/page` 保存。脱敏字段清单位于仓库 `tests/fixtures/form_designer_observed.json`，它是观察证据，不是可启用写入的完整契约。原始配置仅保留于本机 `form-config/capture`，不要复制到技能或日志。

本次请求正文未显示服务端并发戳；请求头未完成核对。页面保存还伴随权限、页面模型、修改时间与站点扩展请求，不能凭 HTTP 204 证明草稿隔离或完整关联保存。站点扩展修改范围未确认，因此不在执行白名单内。默认保持 `write_ready=false`。

本机协议 JSON 由 `contract_path` 指定，绑定平台 Origin、环境、平台和脱敏证据文件 SHA256；只读协议和写入 readiness 分开。写入必须补齐服务端条件更新、草稿隔离、完整身份映射、关联同步及关联回读契约。不能把本地内容哈希当服务端并发戳，也不能通过修改布尔标记替代真实验证。

本机连接窗口保存非秘密字段 `environment`、`base_url`（Origin）、`api_prefix`（通常 `/gxp2`）、`platform`（`web|wap`）和 `contract_path`。独立凭据条目绑定环境及 Origin；切换连接不会把原站点凭据发送到新站点。

协议版本为 `version: 1`，主要字段如下；完整离线示例及拒绝路径见 `tests/test_form_platform.py`，其中合成的并发戳不代表部署平台支持该协议。

| 字段 | 原生协议要求 |
| --- | --- |
| `environment/base_url/platform` | 与独立连接配置逐项相同 |
| `evidence` | `{kind,path,sha256,description}` 数组；路径位于协议文件目录内，读取时重新核对文件哈希 |
| `draft_read` | 原生 `GET`、固定路径、请求模板及 `authoritative_unpublished`；权威性必须有实际依据 |
| `draft_read.response` | `page` 为响应 JSON Pointer，`page_json_string` 控制 Component 解码；写入还需服务端 `revision`、完整 `identity`、`actor`、`associated` 映射 |
| `actor_read` | 如草稿响应没有账号，登记已核实的独立只读账号接口和响应 pointer |
| `conditional_write` | 只有服务端确实强制执行时才登记 `server_enforced/location/name`；本地重复读取不替代原子条件更新 |
| `association_sync/verification` | 登记原生关联保存方式及确定性 `verification.associated` 预期，并能从回读获取实际结果 |
| `write_requests` | 全部实际请求，第一项必须是条件页面保存；身份字段绑定 target，component 绑定完整 candidate；关联请求另绑定当前页面及已验证的 native protocol |
| `catalog` | 组件目录路径、文件 SHA256、`component_version`、`source_fingerprint`；须与原生导出及当前平台一致 |
| `resource_resolvers` | 数据集 `native-dataset-v1`、字典 `native-dictionary-v1` 的原生响应映射；分别需要 `dataset_read/dictionary_read` 证据 |

请求模板只允许服务端本机契约使用 `{"$":"target.page_id"}` 这类字段引用；`{"$json":"candidate"}` 表示将完整候选序列化为原生 Component 字符串。模型不能通过 MCP 传入模板、请求头、URL 或执行 HTTP。

写入证据种类必须包含 `draft_read`、`draft_isolation`、`conditional_write`、`association_sync` 和 `actor`。缺证据、目录漂移、关联请求范围不明或没有完整关联回读时，即使部分读取可用，仍不能保存。

组件属性目录由实际加载的 `Behavior.designerProps.propsSchema` 导出。使用本地 `python -m gxp_form_editor.catalog native-export.json catalog.json --component-version <实际版本> --platform web` 转换元数据；输入文件及组件版本需与目标前端匹配。转换只识别已实现的原生属性与 setter，未知函数、联动和属性映射只读。目录不执行模型或文件提供的脚本。

过滤配置的范围必须分清：组件 `dataFilter` 开关属于本技能；由动作生成的 `dynamicFilter/conditionFilter` 属于编排；数据集 `config.condition` 属于共享数据集。后两者不由表单配置工具修改。不要把条件树写进组件过滤开关。
