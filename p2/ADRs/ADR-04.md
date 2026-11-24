# ADR-04　不可变 GraphVersion 与节点执行复用

**Phase 1.5 决策。** 2A 仅在同一个 Engine 中扫描已安装静态 Workflow 的就绪节点，沿用 checkpoint、Job 和 outbox。下列实例图、单头 CAS、节点 Attempt 和执行键属于后续设计。2A 不新建 `graph_versions` 或 `node_attempts`。[本轮范围](../../V32_PHASE_1_5_DELTA.md) · [源码路径](../../PHASE_2A_SOURCE_MAP.md)

| 决策 | 归属 |
|---|---|
| existing | 保留 Run 的安装版本与 Plugin 组合回执，复用 Engine、SQLite 写事务、checkpoint、Artifact、Job、Lease、预算和 outbox。[Run pins](../../gx-h/contracts/db_schema.sql#L25) · [组合恢复](../../gx-h/gx_core/plugins/runtime.py#L101) · [checkpoint](../../gx-h/gx_core/persistence/store.py#L1445) |
| extend | 2A 扫描全部剩余节点，等待仅阻塞该分支及其后继。所有分支均已终止或在等待且没有就绪工作时，Run 才能整体停车。后续由同一 Engine 读取实例图并核验在途结果归属。[2A 源码路径](../../PHASE_2A_SOURCE_MAP.md) |
| new | 后续动态图与执行身份可用下列两种关系存储。DecisionRecord 沿用 Artifact，采纳与复用关系沿用 checkpoint 和事件。旧科研轮次决策仍按原语义引用。[轮次决策](../../gx-h/gx_core/p0/science_inputs.py#L120) |
| deferred | 跨 Run 结果缓存、生产 Slurm 调度与 checkpoint 互操作留待后续验证。P2 在同一 Run 内验证复用和有故障注入的本地调度对账。 |

## 后续图版本与执行身份

- `graph_versions` 以 `(run_id, version)` 为主键，`(run_id, parent_version)` 外键指向同一 Run 的前版。首版为 1，之后逐次加 1。行保存 `graph_artifact_ref`、`graph_sha` 和 `decision_ref`，加禁止更新和删除的触发器。按主键读最新版本即得 head。图正文保存作者、DecisionRecord 引用、Recipe 摘要、展开后的类型化参数、节点与边、精确的 Plugin／Handler／Capability／Evaluator 摘要、Goal 版本、资源、seed，以及监测、分支、重试和停止规则。[现有不可变记录模式](../../gx-h/contracts/db_schema.sql#L1204)
- `node_attempts` 以 `attempt_id` 为主键，外键绑定原始 Run 和 GraphVersion。记录节点与 fan-out 实例、尝试序号、两个 key、执行身份正文，按需关联真实 Task／Lease、Job 操作、父 Attempt 和 checkpoint Artifact。`(run_id, version, node_instance, attempt_no)` 唯一。同一执行含义允许多次故障重试，故可有多个 Attempt 共用 `exec_key`。原始身份和已结算结果只读。在新图里复用或接管时记录原 Attempt 和产物引用。现有 Job 的 binding 和 execution manifest JSON 使用 `graph_attempt_id` 引用新表的主键。核心 `jobs` 表沿用原结构，原生 Root 继续使用 binding 中的 `attempt_id`。[现有绑定](../../gx-h/gx_core/p0/manifest.py#L27) · [Job 入账](../../gx-h/gx_core/jobs/service.py#L689)

GX 为两种 key 分别标记 schema 版本，以规范化 JSON 计算 SHA-256。`plan_key` 标识某个 Run／图版本中逻辑节点及其展开实例、声明参数和输入绑定。`exec_key` 由实际 Handler 与 Capability 实现摘要、按角色和顺序解析的输入内容摘要、影响输出的参数、随机算法与 seed、相关模型／权重、数值精度与运行环境决定。产生测量的节点还计入 Evaluator 实现摘要。GX 在计算前展开默认值，校验类型和数值有限性。仅供放置的队列、主机提示及整图版本号留在计划记录中。更换 seed 必须产生新的 key、Attempt 和样本批次。调用端附旧 key 时拒绝请求。现有请求哈希带有资源提示，旧 path-step 快捷查找只比较路径、步骤和输入清单。P2 经 Attempt 绑定的 workflow Job 路径提交。[规范化哈希](../../gx-h/gx_core/domain/ids.py#L100) · [请求哈希](../../gx-h/gx_core/jobs/service.py#L360) · [步骤匹配](../../gx-h/gx_core/jobs/service.py#L787)

## 修订、等待与恢复

1. **2B 单头提交。** `amend` 携带 Run、base 版本及 SHA、scope、Decision Lease、草案摘要和幂等键。先冻结输入版本，在 SQLite Writer 外做 schema/图规范化、依赖及影响闭包计算、大对象解析、哈希和文件写入。私有准备不可变决策和图字节，决策引用 base 与草案，图引用预分配的决策 ref。准备好的字节由 ArtifactService 管理，业务只通过已提交引用访问。进入 `BEGIN IMMEDIATE` 后核验 Run/节点状态、当前 head 及输入版本、scope、会话/租约/到期/撤销、Goal/授权、当前预算和幂等记录。若预计算依赖改变则退出并在锁外重算。以条件写入要求恰好增加一个版本，首版须无 head，后续须满足 `head.version = base` 且 SHA 相符。一次提交登记最少的 Artifact 元数据、决策和图引用、checkpoint、回执、事件及 outbox。CAS 失败不产生可见引用或通知，私有暂存按引用清理。每个 Run 严格单头，不做多头或自动合并。精确重放返回原回执。异键竞争者须读取新 head，显式 rebase 再提交。2B 记录冲突数/尝试数、重提次数、额外 token 与耗时、实际重叠的分支/节点。[事务与幂等](../../gx-h/gx_core/persistence/store.py#L994) · [已准备字节的登记](../../gx-h/gx_core/artifacts/service.py#L475) · [Decision Lease](ADR-01.md)
2. 按新旧边计算变更节点及其后继闭包。等待决策只冻结该作用域，Engine 继续推进无关就绪节点。若相关输入和 `exec_key` 均匹配，可复用经验证的已完成 Artifact。对在途工作还须核对当前有效执行租约、Job 身份与准入条件，才可将原 Attempt 接入新图。无法证实归属的 Job 在原 Attempt 上对账结算。其产物若要在新图使用，需重新满足已完成产物的复用条件。[现有 Job 接管](../../gx-h/gx_core/jobs/leases.py#L409) · [产物校验](../../gx-h/gx_core/p0/manifest.py#L231)
3. 修订与 checkpoint 同时提交，记录图引用、分支等待与复用关系，使旧 `advance.id` 失效。跨锁执行的节点在派发或记入进度时核对当前图和推进权。后续外部计算使用 GX 的 `attempt_id`、持久提交的 `operation_id` 和后端的 `scheduler_job_id`。结果未知时沿原操作和 Job 对账。续算须确认父执行终止或已有效撤销，并验证 checkpoint 内容摘要、兼容条件及父 Attempt，才能新增计算和操作。保留父项结果与费用。[Job 合同](ADR-02.md) · [既有提交意图](../../gx-h/gx_core/p0/compute.py#L136)

## 迁移、回退与验收

后续引入动态图时采用原 P0 增量迁移，只有显式选择 Recipe 的新 Run 写入首版，历史 Run 按原工作流与 checkpoint 继续。缺少可验证 seed 或语义输入的记录只作为原实验来源。含新数据的项目由可识别该版本的 GX 对账原 Job，回退的静态项目用对应版本及一致的状态副本。[迁移入口](../../gx-h/gx_core/p0/schema.py#L28) · [历史重复打开证据](../../acceptance/v31/final-at2-preservation.json#L502)

2A 用已有静态图验证一支等待、另一支推进和跨进程恢复。后续图版本与复用再验同 base 竞争、租约撤销后迟交、seed 变更、放置提示变更、单分支参数改变，以及旧推进者或 Job 晚到。核对版本、Attempt、Artifact、实际 Job 数与结算。[长期验收矩阵 A03、A05、A12–A16](../ACCEPTANCE_MATRIX.md)
