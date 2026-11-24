# ADR-05　Codex 原生提交、修复与终态对账

**Phase 1.5 决策。** 2A 保留并回归 v3.1 的原生提交和计量协议。已验证的组合包括 Codex 0.169.0 与 `science_grid@3` 的直接提交。2D 的通用 Terminal Submission 和 Product/Evidence 交付按宿主协议、会话职责、声明的工具和产品/证据 schema 选择，不能要求新插件匹配 Grid 的工作流 ID/版本。[既有启用条件](../../gx-h/gx_core/p0/terminal_products.py#L31) · [范围](../../V32_PHASE_1_5_DELTA.md)

| 决策 | 适用范围 |
|---|---|
| existing | `science_grid@2` 使用原来的普通工具回复。对于 @3，`TeamService` 与 Root 在同一事务提交接受或终局拒绝的裁决、Artifact 引用、回执与终态原因。[产物提交](../../gx-h/gx_core/teams/service.py#L1814) · [调用回执](../../gx-h/gx_core/p0/roots.py#L1034) · [终态记录](../../gx-h/gx_core/p0/terminal_products.py#L75) |
| 2D | 在现有 `Roots`、`TerminalProducts` 和 `AppServerClient` 之间封装直接工具、`NO_TOOL_RESPONSE`、原 turn 中断、回执与冷恢复。复用修复、计量和 Evidence 所有者，按产品声明交付，不要求 Grid 分支。[回调](../../gx-h/gx_core/p0/roots.py#L875) · [协议哨兵](../../gx-h/gx_core/hostobs/app_server_client.py#L703) · [Evidence](ADR-03.md) |
| new | 适配器按官方握手版本、会话类型和直接工具选择行为。业务层只交付可修复错误、普通结果或已持久化的终局结果。P2 Task 和 Decision 各自经兼容测试后才能选择终态优化。 |
| deferred | 未验证的 Codex 版本继续在握手时拒绝该原生会话。GX 保留待处理记录，独立确定性分支和外部 Job 由原所有者继续推进。新宿主的普通回复或终态优化均需各自的兼容回执。[现有版本拒绝用例](../../gx-h/tests/test_p0_roots.py#L270) |

## 提交身份与修复

GX 在回调事务内核验 Run、Root、原 turn 和当前租约。Task 提交交给原 Attempt 的验证器。2B 的 Decision 提案另核验 Decision Lease、scope、base head、Goal 和预算。只有履行本次职责的终局提交才使用直接工具结束 turn，需继续处理的提案返回普通回执。插件 Evaluator 通过类型化 Measurement 向 GX 交付。大对象和科学计算在 Writer 以外准备，GX 回到原事务复核并登记产物。2A 的静态等待不触发新 Decision Lease。[会话作用域](ADR-01.md) · [事务边界](ADR-04.md) · [旧插件提交入口](../../gx-h/gx_core/plugins/runtime_context.py#L165)

结构错误先回滚产品事务，再在独立事务内按官方 Root、turn、call 和请求摘要记录诊断。相同身份和内容重放原回执，标识相同而内容冲突则拒绝。同一 Attempt 可用新 call 提交修正版。达到 `max_product_structural_failures`（默认第 2 个不同的错误 call）才交给现有 Attempt 恢复流程。Job 重试、科学裁决各按原记录结算。GX 对已终局提交发出的中断只结束传输，原裁决及其正式结算继续有效。[修复计数](../../gx-h/gx_core/p0/terminal_products.py#L104) · [中断归类](../../gx-h/gx_core/p0/roots.py#L240)

## 0.169.0 的时限与恢复

已提交的直接调用最多等待 1 秒接收该调用尚缺的 completion。随后在回调外对原 turn 发中断。对账 RPC 超时和重试间隔各为 2 秒。ACK 只记录传输进展，官方结束通知或原 turn 历史才确认终态。仅在重新查得同一 turn 仍为 `inProgress` 时才重发中断。崩溃恢复先读未加载线程的历史与 goal。只有原始回执足以归因、确实缺累计数且 goal 已关闭时，才禁用 goals 并为计量恢复该线程。[原 turn 对账](../../gx-h/gx_core/p0/terminal_products.py#L213) · [协议时限](../../gx-h/gx_core/p0/terminal_products.py#L54) · [冷恢复](../../gx-h/gx_core/p0/roots.py#L681)

provider 缺报的实际用量保持未知。父子用量与累计数都得到证实时，原预留结清一次。对已确认结束但缺用量的 turn，计量宽限期（默认 30 秒）届满后按预留 token 与已知下界的较大值记为 `ESTIMATED`，并保留证据下界。迟到的可靠累计数沿原账本幂等调整。终态传输的 1 秒等待与计量宽限期各自计时。[计量策略](../../gx-h/gx_core/p0/metering.py#L17) · [父子对账](../../gx-h/gx_core/p0/root_delivery.py#L539) · [估算](../../gx-h/gx_core/p0/metering.py#L269) · [迟到调整](../../gx-h/gx_core/p0/metering.py#L166)

**验收与回退。** 2A 重跑终态提交与中断/ACK、同 Attempt 修复、精确幂等重放及缺用量后的恢复。宿主缺席时 Driver 先做确定性工作，待结回执与用量保留原身份。2D 再验非 Grid 插件的直接终态和普通 Product/Evidence 交付。历史 OFFICIAL_HOST_LOOPBACK 有 204 个协议用例，冷恢复对原提交的 105 token 仅结算一次。迟到 ACK 的八例逆序回放属 SCRIPTED。回退时按原会话版本对账其待处理记录。[历史协议](../../acceptance/v31/final-terminal/source.json#L125) · [冷恢复](../../acceptance/v31/official-terminal-crash/rc-9cd6655.json#L492) · [迟到 ACK](../../acceptance/v31/final-terminal/late-receipts.json#L7)
