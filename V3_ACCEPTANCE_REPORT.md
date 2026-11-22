# GX-H v3 签收记录

| 阶段 | 当前记录 | 对应证据 |
|---|---|---|
| 原平台基线 | `61ed006`，全量原用例 340 通过、4 项可选跳过。原生 Codex 环回 30 项通过。复制的旧暂停项目在恢复后沿用在途 Job；终态重放零新增请求。 | [回归](acceptance/v3/baseline-v21/frozen-comparison.json) · [冷恢复](acceptance/v3/staging/control-replay.json) |
| Gate 0 | 2026-11-22 17:42:35 UTC 开始，使用官方 Codex 0.169.0 和 OpenAI OAuth 的两个并发 Root。按 13 波计划采集，每波各有两个 Run。单独统计模型真正执行时间与波次间隔。 | [协议](acceptance/v3/performance/PROTOCOL.md) · [首波实测](acceptance/v3/performance/gate0/first-wave.json) |
| P0 加载与安装 | 通过，产品提交 `1770376`。加载器、操作员安装、同 ID 两个版本共存、原平台回归均有执行回执。 | [P0 报告](V3_P0_REPORT.md) |
| P1 科研工作流接入与派发 | 待完成 Gate 0 后接入并进行匹配的真实模型对照。 | [迁移范围](V3_MIGRATION_MAP.md) |
| P2 独立插件 | 待 P1 签收后验证工具调用、真实计算 Job 与产物。 | [验收条件](V3_ACCEPTANCE_PLAN.md) |
| P3 Run 级固定组合 | 待 P2 签收后验证多版本、冷恢复和回退。 | [验收条件](V3_ACCEPTANCE_PLAN.md) |
