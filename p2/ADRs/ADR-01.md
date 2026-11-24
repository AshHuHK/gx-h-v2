# ADR-01　GX 持久执行与两类 Codex 会话

**Phase 1.5 决策。** GX 持有 Run、Task、Job、预算和产物的权威状态。2A 在已有静态 Workflow 上验证离线推进。完整 Decision Lease 及动态 Graph head 归 2B。PI 拥有 Goal、科学范围和硬预算。[本轮范围](../../V32_PHASE_1_5_DELTA.md) · [2A 源码路径](../../PHASE_2A_SOURCE_MAP.md)

## Existing · 当前写入路径

带 `--run-key` 的启动由本地 operator 提交初始 Intent，再进入 Engine。无 key 的旧启动在首个 checkpoint 前等待原生主线程。主线程处理已确认的人类消息。grid 的 planner 则领取 Task、提交科研决策产品。现行 Task 回调核对 Run、当前 attempt、租约、thread 和 turn。`coordinator_leases` 的一行代表整个 Run 的协调权。[引导](../../gx-h/gx_core/p0/cli.py#L143) · [Engine 启动](../../gx-h/gx_core/orchestration/service.py#L683) · [主线程](../../gx-h/gx_core/p0/driver.py#L121) · [planner](../../gx-h/workflows/science_grid_v3/nodes.py#L291) · [Task 回调](../../gx-h/gx_core/p0/roots.py#L977) · [协调租约](../../gx-h/contracts/db_schema.sql#L85)

## 2B 的会话与 Decision Lease

| 会话 | 冻结输入与允许的操作 |
|---|---|
| Decision Session | GX 在 Run 内以一个明确的 decision scope（待决事项及覆盖的分支/节点）对应一个决策 Task。保存 Goal、证据及预算快照，已有 GraphVersion 时绑定 base 版本和摘要，首版用空 base。Decision Lease 复用该 Task 的 `leases.lease_id`，绑定 owner 和当前 session/thread/turn。允许提议 `submit`、`amend`、`resolve`。 |
| Agent Task Session | GX 将 Root 绑定到普通 Task、attempt、lease 和当前 turn。允许读取获授材料、调用该节点声明的工具、提交本 Task 的产品。GX 对 Graph、Goal 或硬预算写请求返回权限错误。 |

沿用 Team/Lease 的签发、有效期和释放。GX 将同一 `(run_id, decision_scope)` 的同时触发合为一个 Task，同一 scope 同时最多一份有效 Decision Lease。仅当前 owner/session 可在到期前续期，GX 复核 Task、scope、base、授权和未撤销状态，记录新到期时间。到期、接管或 scope 关闭后，旧持有人不能续期或提交新提案。未完成工作和原租约历史可按原 Task 接续。Run 的协调租约继续供 Driver 使用。Native Child 仍由 Codex 管理，按父 Task 的实际授权读取。[Lease 签发、核验与释放](../../gx-h/gx_core/persistence/store.py#L1026) · [Task 租约字段](../../gx-h/contracts/db_schema.sql#L233) · [child 读取](../../gx-h/gx_core/p0/roots.py#L948) · [计量](../../gx-h/gx_core/p0/z_v21_metering.sql#L67)

## 提交、接管与宿主离线

1. **2B 提交。** 提案携带 Run、scope、base、Decision Lease、草案摘要和幂等键。GX 在锁外准备不可变图和决策正文。提交事务核验当前角色、owner/session、thread/turn、scope、租约到期/撤销、base、Goal、预算和幂等记录。首版或 amendment 成功才登记图、决策引用、接纳事件和回执。仅解除等待的 `resolve` 写入该 scope 的裁决和唤醒。同 base 的不同提案争一个 head，冲突者读取新 head 并显式重提。已提交的精确重放可凭原身份读取回执。[提交事务](ADR-04.md) · [现有调用回执](../../gx-h/gx_core/p0/roots.sql#L19)
2. **人工接管。** 本地 operator 在一个事务内先撤销受影响 scope 的 Decision Lease，再登记 PI 接管和决定。未发 lease 时，标记待处理决策已由 PI 接手，防止冷启动补派。撤销后的新提案返回失效且不产生业务写入。硬预算增加、Goal 放宽和科学范围变更由 PI 按其明确授权另行处理。其他 scope 的任务和在途 Job 继续按原所有者对账。[本地 operator](../../gx-h/gx_core/p0/operator.py#L26) · [释放与校验](../../gx-h/gx_core/persistence/store.py#L1066)
3. **首版图前。** GX 沿既有 Run、Intent、预算、插件组合回执和 operator 记录持久化 Goal、硬预算、插件组合及初始化授权，重启能恢复同一组引用及原 run key。可在 Codex 缺席时等待首次决策。2A 使用已安装静态工作流，后续引导可选风险最低的既有表示，无须注册虚构科研 Workflow 或新建 Run 状态机。[现有引导](../../gx-h/gx_core/p0/cli.py#L143) · [组合恢复](../../gx-h/gx_core/plugins/runtime.py#L101)
4. **2A 离线。** GX 先处理持久事件/outbox，对账已提交的 Job，推进就绪节点并完成产物与结算，之后才考虑 Codex 调度。已有计量保持与原 turn 的绑定。宿主缺席、启动失败或断连，仅相关节点/分支持久保存等待 owner、引用、原因、时间及适用的重试时间，通知可去重。可区分 Job、Decision、Agent Task 和 Human 等待，未派发也可有等待记录。独立节点继续，有可执行节点时 Run 保持推进。重启沿原 checkpoint 和 Job 身份查询。2A 的等待记录不授予 Decision Lease。[2A 源码路径](../../PHASE_2A_SOURCE_MAP.md) · [outbox](../../gx-h/contracts/db_schema.sql#L1028)

## 验收与回退

2A 验证 A01、A14、宿主缺席且零拉起、混合状态下跨进程重启与原 Job 对账。2B 再验证无 head 的首次决策、同 scope 竞争、过期/撤销后迟交、普通 Task 越权及 PI 接管。历史 CLI 的 0、3、0 次脚本模型响应仅是 v3.1 的启动、运行和重放记录。[历史 CLI 回执](../../acceptance/v31/final-public-deterministic.json#L19)

2A 的静态 Run 依据既有 Workflow pin、checkpoint、Job、预留和计量回执恢复。后续已创建实例图的项目需要能读取该图的版本对账。回退保留原外部执行和费用，防止从旧副本重复提交。[图迁移边界](ADR-04.md)
