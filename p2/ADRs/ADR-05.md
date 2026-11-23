# ADR-05　Codex 原生提交、修复与终态对账

**状态　提案，待审阅。** GX 负责判定并持久化科研产物或决策提案的处理结果。版本化的 `CodexProtocolAdapter` 负责把已提交的业务结果送回原线程，或结束其原 turn。v3.1 有回执的组合是 Codex 0.169.0 与 `science_grid@3` 的直接提交。[版本核验](../../gx-h/gx_core/p0/roots.py#L149) · [现行启用条件](../../gx-h/gx_core/p0/terminal_products.py#L31)

| 决策 | 适用范围 |
|---|---|
| existing | `science_grid@2` 使用原来的普通工具回复。对于 @3，`TeamService` 与 Root 在同一事务提交接受或终局拒绝的裁决、Artifact 引用、回执与终态原因。[产物提交](../../gx-h/gx_core/teams/service.py#L1814) · [调用回执](../../gx-h/gx_core/p0/roots.py#L1034) · [终态记录](../../gx-h/gx_core/p0/terminal_products.py#L75) |
| extend | 在现有 `Roots`、`TerminalProducts` 和 `AppServerClient` 之间封装直接工具配置、`NO_TOOL_RESPONSE`、原 turn 中断、冷恢复及 driver 的待处理终态。复用既有修复表、回执和计量账本。[回调](../../gx-h/gx_core/p0/roots.py#L875) · [协议哨兵](../../gx-h/gx_core/hostobs/app_server_client.py#L703) · [待对账记录](../../gx-h/gx_core/p0/driver.py#L290) |
| new | 适配器按官方握手版本、会话类型和直接工具选择行为。业务层只交付可修复错误、普通结果或已持久化的终局结果。P2 Task 和 Decision 各自经兼容测试后才能选择终态优化。 |
| deferred | 未验证的 Codex 版本继续在握手时拒绝该原生会话。GX 保留待处理记录，独立确定性分支和外部 Job 由原所有者继续推进。新宿主的普通回复或终态优化均需各自的兼容回执。[现有版本拒绝用例](../../gx-h/tests/test_p0_roots.py#L270) |

## 提交身份与修复

GX 在回调事务内核验 Run、Root、原 turn 和当前租约。Task 提交交给绑定于该 Attempt 的验证器。Decision 提案须通过 Decision Lease、分支、base head 及 Goal 约束的检查。需要继续处理的提案返回普通回执。只有履行了本次职责的终局提交才能通过直接工具结束 turn。宿主的 thread goal 只控制该线程。P2 插件 Evaluator 返回 typed Measurement，由 GX 按 Goal 判定。旧插件接口可把结果转交 `_submit`，因此 P2 应将度量回调与最终提交分开。[Task 绑定](../../gx-h/gx_core/p0/roots.py#L69) · [直接工具命名空间](../../gx-h/gx_core/p0/roots.py#L527) · [旧插件转交入口](../../gx-h/gx_core/plugins/runtime_context.py#L165)

结构错误先回滚产品事务，再在独立事务内按官方 Root、turn、call 和请求摘要记录诊断。相同身份和内容重放原回执，标识相同而内容冲突则拒绝。同一 Attempt 可用新 call 提交修正版。达到 `max_product_structural_failures`（默认第 2 个不同的错误 call）才交给现有 Attempt 恢复流程。Job 重试、科学裁决各按原记录结算。GX 对已终局提交发出的中断只结束传输，原裁决及其正式结算继续有效。[修复计数](../../gx-h/gx_core/p0/terminal_products.py#L104) · [中断归类](../../gx-h/gx_core/p0/roots.py#L240)

## 0.169.0 的时限与恢复

已提交的直接调用最多等待 1 秒接收该调用尚缺的 completion。随后在回调外对原 turn 发中断。对账 RPC 超时和重试间隔各为 2 秒。ACK 只记录传输进展，官方结束通知或原 turn 历史才确认终态。仅在重新查得同一 turn 仍为 `inProgress` 时才重发中断。崩溃恢复先读未加载线程的历史与 goal。只有原始回执足以归因、确实缺累计数且 goal 已关闭时，才禁用 goals 并为计量恢复该线程。[原 turn 对账](../../gx-h/gx_core/p0/terminal_products.py#L213) · [协议时限](../../gx-h/gx_core/p0/terminal_products.py#L54) · [冷恢复](../../gx-h/gx_core/p0/roots.py#L681)

provider 缺报的实际用量保持未知。父子用量与累计数都得到证实时，原预留结清一次。对已确认结束但缺用量的 turn，计量宽限期（默认 30 秒）届满后按预留 token 与已知下界的较大值记为 `ESTIMATED`，并保留证据下界。迟到的可靠累计数沿原账本幂等调整。终态传输的 1 秒等待与计量宽限期各自计时。[计量策略](../../gx-h/gx_core/p0/metering.py#L17) · [父子对账](../../gx-h/gx_core/p0/root_delivery.py#L539) · [估算](../../gx-h/gx_core/p0/metering.py#L269) · [迟到调整](../../gx-h/gx_core/p0/metering.py#L166)

**验收与回退。** A17 重跑同一 call 重放、同 Attempt 修复、COMMIT 后崩溃、丢失与迟到 ACK，以及缺 provider 用量用例。另测未知版本拒绝、P2 Task/Decision 各自的终局提交和普通 Measurement 回调。历史 `OFFICIAL_HOST_LOOPBACK` 有 204 个协议用例，最终冷恢复原提交的 105 token 只结算一次，新增模型请求为 0。八个丢失 ACK 的逆序回放另属 `SCRIPTED`。回退时停止给新会话选择待撤回的组合，原回执、预留和待对账调用由原版本继续处理。[冻结版协议](../../acceptance/v31/final-terminal/source.json#L125) · [冷恢复回执](../../acceptance/v31/official-terminal-crash/rc-9cd6655.json#L492) · [迟到 ACK](../../acceptance/v31/final-terminal/late-receipts.json#L7)
