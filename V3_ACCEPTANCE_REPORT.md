# GX-H v3 签收记录

| 阶段 | 当前记录 | 对应证据 |
|---|---|---|
| 原平台基线 | `61ed006`，全量原用例 340 通过、4 项可选跳过。原生 Codex 环回 30 项通过。复制的旧暂停项目在恢复后沿用在途 Job；终态重放零新增请求。 | [回归](acceptance/v3/baseline-v21/frozen-comparison.json) · [冷恢复](acceptance/v3/staging/control-replay.json) |
| Gate 0 | 通过。首个原生 turn 到末次完成相隔 7,321.716 秒，其中 1,583.510 秒有模型执行，两 Root 重叠 1,438.667 秒。26 个 Run、52 个 CPU Job 全部完成，78 个 turn 对账为 8,647,052 tokens。 | [协议](acceptance/v3/performance/PROTOCOL.md) · [完整基线](acceptance/v3/performance/gate0/REPORT.md) |
| P0 加载与安装 | 通过，产品提交 `1770376`。加载器、操作员安装、同 ID 两个版本共存、原平台回归均有执行回执。 | [P0 报告](V3_P0_REPORT.md) |
| P1 科研工作流接入与派发 | 代码、四组科学回放及两小时实测已完成。两个版本各完成 26 个科研决策和 52 个 Job。原始回调及派发 p95 超过复核门槛，性能签收状态为 REVIEW。 | [P1 报告](V3_P1_REPORT.md) · [两小时比较](acceptance/v3/performance/COMPARISON.md) |
| P2 独立插件 | 待 P1 签收后验证工具调用、真实计算 Job 与产物。 | [验收条件](V3_ACCEPTANCE_PLAN.md) |
| P3 Run 级固定组合 | 待 P2 签收后验证多版本、冷恢复和回退。 | [验收条件](V3_ACCEPTANCE_PLAN.md) |
