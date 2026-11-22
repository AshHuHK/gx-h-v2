# GX-H v3 签收记录

| 阶段 | 当前记录 | 对应证据 |
|---|---|---|
| 原平台基线 | `61ed006`，全量原用例 340 通过、4 项可选跳过。原生 Codex 环回 30 项通过。复制的旧暂停项目在恢复后沿用在途 Job；终态重放零新增请求。 | [回归](acceptance/v3/baseline-v21/frozen-comparison.json) · [冷恢复](acceptance/v3/staging/control-replay.json) |
| Gate 0 | 通过。首个原生 turn 到末次完成相隔 7,321.716 秒，其中 1,583.510 秒有模型执行，两 Root 重叠 1,438.667 秒。26 个 Run、52 个 CPU Job 全部完成，78 个 turn 对账为 8,647,052 tokens。 | [协议](acceptance/v3/performance/PROTOCOL.md) · [完整基线](acceptance/v3/performance/gate0/REPORT.md) |
| P0 加载与安装 | 通过，产品提交 `1770376`。加载器、操作员安装、同 ID 两个版本共存、原平台回归均有执行回执。 | [P0 报告](V3_P0_REPORT.md) |
| P1 科研工作流接入与派发 | 代码、四组科学回放及两小时实测已完成。两个版本各完成 26 个科研决策和 52 个 Job。原始回调及派发 p95 超过复核门槛，性能签收状态为 REVIEW。 | [P1 报告](V3_P1_REPORT.md) · [两小时比较](acceptance/v3/performance/COMPARISON.md) |
| P2 独立插件 | 尚未实施。待 P1 签收后，用第二个独立插件验证工具调用、真实计算 Job 与产物。 | [P2 状态和用例](V3_P2_REPORT.md) |
| P3 Run 级固定组合 | 尚未实施。待 P2 签收后，在同一工作区验证双版本科研执行、组合校验及缺失版本恢复。 | [P3 状态和用例](V3_P3_REPORT.md) |

## 部署与恢复

可单独部署的 Linux x86_64 快照来自产品提交 `2476f2b`。归档重建得到相同的 SHA-256。从空目录安装后，24 条检查命令均成功，覆盖官方 Codex 0.169.0、插件公开核验、选择插件后启动并暂停新项目，以及从副本读取旧 v2.1 项目。该部署检查使用了零次模型调用。另一次完整冷恢复实测核对了原 Job 回执，项目抵达终态后再打开时新增模型请求为零。[部署与启动](V3_DEPLOYMENT_REPORT.md) · [部署回执](acceptance/v3/p1/standalone-deployment.json) · [恢复与回退](V3_ROLLBACK_REPORT.md) · [已知限制](V3_KNOWN_LIMITATIONS.md)

真实模型实测中，提交回调 p95 从 65.7 ms 升至 97.5 ms。使用 7 份真实提交和原始状态重新交错执行，在源代码和数据位于同一共享文件系统时，每版各 168 次无探针计时的 p95 分别为 44.9 ms 和 42.8 ms。复验限定了在这些条件下可重现的开销，付费阶段的历史差值仍待解释。实施包要求上一阶段通过后再开始下一阶段，v2.1 继续作为稳定回退版本。[实测比较](acceptance/v3/performance/COMPARISON.md) · [真实慢样本回放](acceptance/v3/p1/callback-owner-audit/paid-replay/REPORT.md) · [实施包阶段规则](GX_H_V3_Lean_Implementation_Package/GX_H_V3_BLUEPRINT.md)
