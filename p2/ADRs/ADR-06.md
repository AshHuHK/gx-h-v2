# ADR-06　角色工具目录、子线程与任务简报

**Phase 1.5 决策。** 2A 回归已冻结的角色工具、Native Child 和 Brief。2D 的 Role Tool Catalog 及 Task Brief/Artifact Preloading 由任务职责、插件声明与获准引用驱动。通用路径不得要求 `science_grid@3` 特判。已有 Grid 项目沿其原工作流和 Attempt 恢复。[既有派工](../../gx-h/gx_core/p0/roots.py#L353) · [范围](../../V32_PHASE_1_5_DELTA.md)

| 决策 | 适用范围 |
|---|---|
| existing | `science_grid@3` 的 reviewer/planner 可取当前任务、读 Artifact、查看网格测量并提交产品。executor 可检查能力、提交、查询和等待 Job，researcher 另可取消 Job。显式岗位配置可指定独立计算或原生委派。GX 保存实际目录并设置官方 `agents.enabled` 和对应功能位。[角色目录](../../gx-h/gx_core/p0/profiles.py#L162) · [宿主配置](../../gx-h/gx_core/p0/roots.py#L500) |
| 2D | 依分配的能力和职责选取已注册的计算、度量、读取或提交工具。沿用服务端引用授权、原生子线程计量和简报渲染。Product/Evidence 交付按产品声明选择并进入相应 GX 所有者。[注册工具](../../gx-h/gx_core/plugins/bindings.py#L71) · [授权读取](../../gx-h/gx_core/p0/roots.py#L1045) · [交付协议](ADR-05.md) |
| new | 将两类 Session 的作用域和当前 GraphVersion、分支、Goal 版本接入现有回调与上下文。GX 根据当前作用域验证每项新操作，单独判断已提交调用的回执重放。简报和历史摘要沿用现有存储及只读投影。 |
| deferred | 新宿主的子线程、独立插件的角色目录及简报效率结论须分别有本版本证据。既有运行保留原版本、已领 Attempt 的简报和作用域。 |

## 服务端授权

- **Agent Task Session。** 回调匹配 Run、Task、当前 Attempt、有效 Lease、官方 thread 与活动 turn，随后核验工具和所读 Artifact 的授权引用。planner 在 Task 内提交科研判断。GX 拒绝修改 Graph、Goal 或硬预算的请求。获得计算工具的任务仍由既有 Job 与 Budget 所有者准入。[租约绑定](../../gx-h/gx_core/p0/roots.py#L69) · [当前目录与租约](../../gx-h/gx_core/p0/roots.py#L977)
- **Decision Session（2B）。** 允许读取授权分支的证据并提出 submit、amend、resolve。提交时核验既有 Lease owner 签发的同 Run/scope 租约、持有人/session、thread/turn、base、到期/撤销、Goal 和硬预算。接管后拒绝迟交的新提案。冲突时读取新 head 后显式 rebase，已提交调用的精确重放返回原回执。[租约有效期和唯一性](ADR-01.md) · [提交事务](ADR-04.md)
- **Native Child。** 经确认的直接子线程可在活动 turn 中读取父 Task 已获授权的 Artifact。GX 核对父线程的 spawn 活动、子线程元数据和原任务的有效租约。漏掉开始通知时可据官方历史恢复，核实期间返回可重试的读取错误。普通 fork、其他 Run、无关引用、过期租约和写入请求均拒绝。更深后代的用量按因果根 turn 对账，读取授权只授予直接子线程。[漏通知恢复](../../gx-h/gx_core/p0/roots.py#L293) · [子线程鉴别](../../gx-h/gx_core/p0/root_delivery.py#L874) · [读取判定](../../gx-h/gx_core/p0/roots.py#L948)

## 快照、权威历史与重读

预载限于任务已获授权、内容不可变并经原始字节 SHA-256 核对的小型 JSON 摘要，以 Artifact ref 为键携带哈希值和内容。单项原文、预载原文字节合计及序列化后的预载映射各限 16 KiB。数组、原始观测、论文及较大内容通过授权引用按需读取。首个用户 turn 携带快照，恢复时可通过新一次 `gx_task_context` 调用读取当前状态和新增引用。[选择与大小检查](../../gx-h/gx_core/p0/grid_brief.py#L56) · [恢复提示](../../gx-h/gx_core/p0/roots.py#L799) · [实时引用授权](../../gx-h/gx_core/p0/roots.py#L1004)

P2 简报同时携带 Run、分支、快照所依据的 GraphVersion 和 Goal 版本及摘要。GX 从已提交的图版本、Decision、Attempt、Job、Measurement、Evidence 和 Artifact 引用生成摘要，保留失败、异议及待签署状态。Agent 笔记可作为带来源的材料，权威状态只由相应 GX 记录提供。收到变更通知或提交时发现引用已过期，应重新读取当前上下文和受影响 Artifact。Decision 的 base head 冲突后须基于新 head 重提。Task 按当前分支的输入和生效 Goal 核验，其他分支更新时仍可复用语义依赖相同的工作。旧快照保留其历史版本。[已有 Attempt 历史](../../gx-h/gx_core/teams/recovery.py#L203) · [已接受审阅与来源覆盖](../../gx-h/gx_core/research/records.py#L105) · [现有任务版本校验](../../gx-h/gx_core/p0/science_inputs.py#L238)

**验收与回退。** 2A 核对 v3.1 的 native child 读取作用域、恢复和用量，以及 Brief/Artifact 预载的摘要、三项字节限制与当前引用。独立插件上 A18、A19 的角色目录与简报归入通用协议的后续验收。历史 OFFICIAL_HOST_LOOPBACK 记录 8 个角色目录、19 项 child 检查，以及迟到来源后沿用两项 Job。历史离线重放曾核对 338 份摘要。回退按保存的工作流和原 Attempt 恢复目录、简报及引用授权。[宿主角色与子线程](../../acceptance/v31/b2/child-recovery/final-official-lost-start.json#L208) · [迟到来源](../../acceptance/v31/brief-wire.json#L250) · [离线重放](../../acceptance/v31/brief-replay.json#L4)
