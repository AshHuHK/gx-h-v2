# P2A Closure 集成验收

**暂定结论，最终冻结版尚待验证。** 首轮集成诊断从干净的 `v3.2-p2` 提交 `d5df10e7ff4d93e3f3e104c042f797eb92ad49d6` 启动。当前已记录两项需要修复的现象。官方终态协议探针在 204 回合序列中遇到缺少预期取消通知的时序。原 Python 套件在两个 CPU Job 插槽都已占用时，混合工作区的一次 `resume` 抛出 `RESOURCE_DENIED`。474 个发现用例中有 469 通过、1 个错误、4 个原有可选 SDK 跳过。[本次运行日志](var/acceptance/full-d5df10e-20261124T1533Z.log) · [版本与环境](var/acceptance/full-d5df10e-20261124T1533Z/environment.json)

## 首轮诊断

| 范围 | 已签发的结果 |
|---|---|
| 原 Phase 2A | P2A-01–08、13、14、15 通过。P2A-09 的终态协议失败。P2A-10、11、12 及 REG01 依赖共用套件，沿用该套件的失败。 |
| Closure | U01–U08、T01、T02、DB01、DB02、AC01–AC03 和 IDLE01–IDLE03 通过。T03 的逐项回放遇到上述真实的工作区错误。 |
| 附加检查 | REAL_RESOURCES 通过。OFFICIAL-USAGE 与 LEGACY-LEDGER 以本轮完整运行清单签发的结果为准。 |

首轮跨进程观测记录了停止官方 Codex 宿主后的静态分支执行，宿主再启动、重复节点、重复 Artifact 和重复结算均为零。独立的旧式 extra-child 探针保存了冷恢复的 `COLD_SETTLEMENT_TIMEOUT` 和内层退出码 1。它的原始观测与后续按新成本合同进行的回放均予以保留。[跨进程观测](var/acceptance/full-d5df10e-20261124T1533Z/raw/cross-process/evidence/observations.json) · [冷恢复原始记录](var/acceptance/full-d5df10e-20261124T1533Z/raw/v31-regression/evidence/extra-child/observation.json)

## 已有专项证据

- 同一子线程四笔官方响应合计 732 个 token，公开的 915 是该线程累计值。历史回放把 183 留在 `UNATTRIBUTED_DELTA`，另列父线程三笔合计 105。首次专项 U01–U08 有自己的产品 SHA、原始事件与账本。[用量对账专项](P2A_09_USAGE_RECONCILIATION_REPORT.md)
- 独立的 465 项固定环境复跑曾得到 461 通过、4 个原有跳过。所提供历史材料只给出另一次 42 个失败的总数，没有可供逐项关联的身份和错误。原失败逐项归因仍待原始记录。[环境与历史输入范围](P2A_TEST_ENVIRONMENT_REPORT.md)
- 真实 SQLite 连接竞争写锁、命令和日志归档、Driver 空闲唤醒及到期维护分别有独立回执。早期 AC02 断言失败及其仅失败项重跑均保有原包。[SQLite](P2A_SQLITE_TRANSACTION_TELEMETRY_REPORT.md) · [归档与重跑](P2A_ACCEPTANCE_AUTOMATION_REPORT.md) · [Driver](P2A_IDLE_BACKOFF_REPORT.md)

最终签收以完整冻结版的提交、全部 38 项状态、执行命令和可解包 SHA-256 清单为依据。证据范围包括脚本化输入、官方宿主环回及真实本机 CPU Job。原始历史运行、恢复办法和仍缺失的输入见专项记录。[限制与回退步骤](P2A_CLOSURE_LIMITATIONS.md) · [Phase 2A 历史签收](PHASE_2A_ACCEPTANCE_REPORT.md)
