# ADR-01　GX 持久执行与两类 Codex 会话

**Phase 1 提案，待审阅。** 基线 `91ea828`。GX 持有 Run、Graph head、Task、Job、预算和产物的写权。现有 `gx-p0 run` 驱动 GX，Codex 仅在需要决策或执行有界科研任务时接入。人类 PI 拥有 Goal Contract、科学范围和硬预算上限。[阶段 0 审计](../../V32_SOURCE_AUDIT.md)

## Existing · 当前写入路径

带 `--run-key` 的启动由本地 operator 提交初始 Intent，再进入 Engine。无 key 的旧启动在首个 checkpoint 前等待原生主线程。主线程处理已确认的人类消息。grid 的 planner 则领取 Task、提交科研决策产品。现行 Task 回调核对 Run、当前 attempt、租约、thread 和 turn。`coordinator_leases` 的一行代表整个 Run 的协调权。[引导](../../gx-h/gx_core/p0/cli.py#L143) · [Engine 启动](../../gx-h/gx_core/orchestration/service.py#L683) · [主线程](../../gx-h/gx_core/p0/driver.py#L121) · [planner](../../gx-h/workflows/science_grid_v3/nodes.py#L291) · [Task 回调](../../gx-h/gx_core/p0/roots.py#L977) · [协调租约](../../gx-h/contracts/db_schema.sql#L85)

## Extend · 由 GX 签发会话权限

| 会话 | 冻结输入与允许的操作 |
|---|---|
| Decision Session | GX 为 Run 内一个待决范围创建决策类型 Task，保存 `base_version`、图的 Artifact 摘要、Goal 版本、证据引用与摘要、预算快照。Decision Lease 取该 Task 的当次 `leases.lease_id`。会话可提出 `submit`、`amend`、`resolve`，由 GX 用当前 head 和剩余额度验证。首次建图的 base 表示尚无 head。 |
| Agent Task Session | GX 将 Root 绑定到普通 Task、attempt、lease 和当前 turn。允许读取获授材料、调用该节点声明的工具、提交本 Task 的产品。GX 对 Graph、Goal 或硬预算写请求返回权限错误。 |

服务端从 GX 签发的 Task 和当前 Root 绑定判定会话类型。同一 scope 的触发合并到一个待决 Task，沿用团队与预算的并发约束。Run 协调租约服务协调器。Codex 管理 native child，GX 在每次读取时验证直接父 Root、活租约、子 turn 和获授 Artifact。已有 Root、投递与用量回执关联 driver 及版本、请求和实际 provider/model/effort、thread/turn、终态和用量来源，缺失与估算分别标记。工作者租约的 attempt 与计算 Job 的 `node_attempts` 各有独立身份。[Task 租约](../../gx-h/contracts/db_schema.sql#L233) · [绑定及实际配置](../../gx-h/gx_core/p0/roots.py#L546) · [child 读取](../../gx-h/gx_core/p0/roots.py#L948) · [用量](../../gx-h/gx_core/p0/z_v21_metering.sql#L67)

## New · 提案提交、接管与断连

1. **提交。** 新提案携带 Run、scope、`base_version`、Decision Lease、草案摘要和幂等键。GX 在同一写事务内复核服务器签发的决策类型、当前 lease/thread/turn、Goal、预算与 head。建图或 amendment 将已校验的不可变 Graph Artifact 记入 `graph_versions`，以 base 作比较并交换 head，决策正文使用不可变 Artifact，同事务写入引用该决策正文、新图及提交回执的接纳事件。仅解除待决事项的 `resolve` 记录裁决并唤醒该 scope。相同 base 的并发改图只有一方成功，另一方读取新 head 后 rebase。相同已提交调用可查询原回执。[Task 活租约核对](../../gx-h/gx_core/p0/roots.py#L69) · [现有调用回执](../../gx-h/gx_core/p0/roots.sql#L19)
2. **人工接管。** 本地 operator 在一个事务内先撤销受影响 scope 的 Decision Lease，再登记 PI 接管和决定。未发 lease 时，标记待处理决策已由 PI 接手，防止冷启动补派。撤销后的新提案返回失效且不产生业务写入。硬预算增加、Goal 放宽和科学范围变更由 PI 按其明确授权另行处理。其他 scope 的任务和在途 Job 继续按原所有者对账。[本地 operator](../../gx-h/gx_core/p0/operator.py#L26) · [释放与校验](../../gx-h/gx_core/persistence/store.py#L1066)
3. **无 Codex。** P2 冷启动先按本地 PI 输入或已存 Goal 打开 Run。既有入口需要工作流 pin，因此显式 Recipe 路径使用受 GX 控制、固定摘要的引导工作流，在同一 Engine 先持久化初始 Decision Task 和 checkpoint；此时实例图为空，首版 CAS 后才运行插件节点。GX 从 checkpoint 恢复，在每轮核对在途 Job、推进就绪的确定性分支，然后按需连接 Codex。找不到可执行文件、初始化失败或中途断连时，Engine 在 checkpoint 的分支状态中持久保存待决 scope 的 `WAITING_DECISION`、base、原因和下次尝试时间，同事务向原 outbox 写入按 scope 和触发原因去重的通知。此时可以尚无 Root。原生用量或未确认投递留待按原身份对账。独立分支可产出新结果。GX 进程也退出时，重启从记录的等待、Job 身份和检查点接续。[当前启动异常](../../gx-h/gx_core/hostobs/app_server_client.py#L167) · [现有重试](../../gx-h/gx_core/p0/coordinator.py#L183) · [当前维护次序](../../gx-h/gx_core/p0/driver.py#L323) · [Job 与 reactor](../../gx-h/gx_core/p0/driver.py#L378) · [单一 Run 中断](../../gx-h/gx_core/orchestration/engine.py#L1699) · [outbox](../../gx-h/contracts/db_schema.sql#L1028)

## Deferred · 验收与回滚

Phase 2 再用确定性故障用例验证冷启动缺 Codex、运行中断连、同一 Run 的独立分支结果、普通 Task 的越权请求及接管后迟到提案。历史 CLI 回执记录了带 key 启动、运行、重放分别使用 0、3、0 次脚本模型响应，只作为 v3.1 比较依据。[历史 CLI 回执](../../acceptance/v31/final-public-deterministic.json#L19)

实现阶段以 P2 Recipe Run 为新行为的启用边界。回滚时先停接新 P2 Run 和原生决策派发，保留待决 scope、租约历史、Graph Artifact 与事件，由持有原状态的 GX 对账在途 Job。旧版本继续使用原 v3.1 状态或升级前的独立副本。含 P2 执行的状态保留给能识别该版本的 GX 恢复，防止从旧副本重提已受理的 Job。
