# Write 离线准确性实施与验收

本批补强当前 runner 报告的内部一致性校验，并建立实际执行 JS 的固定业务语料。没有连接业务库、启动审批、保存或发布业务草稿，也没有安装插件。

## 实现

`logic_review.py` 只对当前 runner 声称 `passed` 的结果，重算数据型 oracle：完整且唯一的断言名称、覆盖、固定期望、实际值、存在性、类型、调用次数和顺序。断言与最终观察值、执行记录必须一致；不重新执行 JS。JSON 对象顺序不影响判定，布尔值与数字区分，`1` 与 `1.0` 按 JS 数字语义相等。

`failed` 和 `unverified` 可以合法缺少完整断言；超时、不支持 API、不可执行 C# 及证据归因不足仍保持原三态语义。旧 runner 的报告可以只读，已有写入版本门槛继续阻止其进入执行。内部一致性校验不证明观察值来自真实执行；来源摘要、独立编译、所有权及人工签名仍是独立边界。

## 实测结果

完整记录见 [metadata](business-accuracy-results-2026-10-26.json)。固定期望来自脱敏需求，在运行候选前写入 [语料](../tests/business_accuracy_cases.py)，判分来自实际 runner 输出；不以源码字符串匹配作准确性结论。

| 层次 | 本次结果 |
| --- | --- |
| 业务错改 | 8 类 × 3 版本 = 24；8 个正确版本无误阻，16 个错误版本全部出现 failed；误绿 0，支持范围内 unverified 0 |
| 不支持行为 | 3 个独立样例全部 unverified，单独统计；未当作检出成功或运行验收通过 |
| 需求归因 | 12/12 符合预期；组、分支、字段、节点、事件覆盖及 C# 分开；配置通过不提升运行状态 |
| 证据变旧 | 9/9 拒绝；候选画布/JS/relations、基线画布/JS、编译器、固定期望/目标、缺失当前断言 |
| 原生跨模式 | 6 种子 × full/incremental/auto = 18/18 符合预期；可接受结果均独立再生成复验 |

6 个原生种子包含支持范围内的字段写入、保留旧引用、复制旧引用、跨组新增旧引用、不可达引用变可达、调用依赖闭包。9 个负例均要求结构化 `undeclared_identifier` 和正确标识符，基础设施错误不能充当成功检出。保留旧引用的 3 个结果是“编译一致性已验证、业务执行未验证”，没有把历史问题变为通过。

每个业务 suite 只有 1–3 个场景，低于 20 个案例和 20 秒预算。这里的“误绿”为错误版本被标为 `passed`；`unverified` 进入人工审阅不计为误绿。这些结果只说明固定合成语料的检出能力，不是模型在线意图识别率、真实业务准确率或生产运行证明。技能路由语料由主任务另行记录。本批没有测量提速。

## 复跑

```powershell
python -B scripts/evaluate_business_accuracy.py --frontend G:/hoyi/updateComponents/gxp2.components --output output/business-accuracy.json --native
```

仅 runner 和内部证据检查可省略 `--native`。现有 Node、前端 parser 和原生桥是前置条件，不会自动安装。原生测试另通过 `GXP_TEST_COMPILER_BRIDGE` 显式启用。

`tests/test_scenario_oracle.py` 覆盖缺失/null/空断言、遗漏、重复、错名称、错期望、错实际值、错存在性、返回值 null/undefined、数字语义和真实 runner 完整正例；`tests/test_business_accuracy_corpus.py` 运行上述固定业务、归因及变旧证据语料。
