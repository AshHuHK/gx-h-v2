# ADR-04　不可变 GraphVersion 与节点执行复用

**状态　待审阅。** 基线 `91ea828` 已将静态工作流的版本与源码摘要固定在 Run 上。P2 提议让同一个 GX Engine 执行可修订的实例图，每次修订生成一个不可变版本。下列行为尚待实现和验收。

| 决策 | 归属 |
|---|---|
| existing | 保留 Run 的安装版本与 Plugin 组合回执，复用 Engine、SQLite 写事务、checkpoint、Artifact、Job、Lease、预算和 outbox。[Run pins](../../gx-h/contracts/db_schema.sql#L25) · [组合恢复](../../gx-h/gx_core/plugins/runtime.py#L101) · [checkpoint](../../gx-h/gx_core/persistence/store.py#L1445) |
| extend | Engine 从当前 GraphVersion 解析节点、选取依赖已满足的分支、校验在途结果归属。现有循环只取第一个剩余节点，park 改变整条 Run 的状态，rewind 丢弃拓扑后缀。[执行](../../gx-h/gx_core/orchestration/engine.py#L1380) · [park](../../gx-h/gx_core/orchestration/engine.py#L1699) · [rewind](../../gx-h/gx_core/orchestration/engine.py#L1676) |
| new | 增加下述两张 P2 表。DecisionRecord 使用现有 Artifact，版本采纳与复用关系记入现有 checkpoint 和事件。现有科研轮次决策可因新资料更新，仍按其原有语义引用。[轮次决策](../../gx-h/gx_core/p0/science_inputs.py#L120) |
| deferred | 跨 Run 结果缓存、生产 Slurm 调度与 checkpoint 互操作留待后续验证。P2 在同一 Run 内验证复用和有故障注入的本地调度对账。 |

## 版本和 Attempt

- `graph_versions` 以 `(run_id, version)` 为主键，`(run_id, parent_version)` 外键指向同一 Run 的前版。首版为 1，之后逐次加 1。行保存 `graph_artifact_ref`、`graph_sha` 和 `decision_ref`，加禁止更新和删除的触发器。按主键读最新版本即得 head。图正文保存作者、DecisionRecord 引用、Recipe 摘要、展开后的类型化参数、节点与边、精确的 Plugin／Handler／Capability／Evaluator 摘要、Goal 版本、资源、seed，以及监测、分支、重试和停止规则。[现有不可变记录模式](../../gx-h/contracts/db_schema.sql#L1204)
- `node_attempts` 以 `attempt_id` 为主键，外键绑定原始 Run 和 GraphVersion。记录节点与 fan-out 实例、尝试序号、两个 key、执行身份正文，按需关联真实 Task／Lease、Job 操作、父 Attempt 和 checkpoint Artifact。`(run_id, version, node_instance, attempt_no)` 唯一。同一执行含义允许多次故障重试，故可有多个 Attempt 共用 `exec_key`。原始身份和已结算结果只读。在新图里复用或接管时记录原 Attempt 和产物引用。现有 Job 的 binding 和 execution manifest JSON 使用 `graph_attempt_id` 引用新表的主键。核心 `jobs` 表沿用原结构，原生 Root 继续使用 binding 中的 `attempt_id`。[现有绑定](../../gx-h/gx_core/p0/manifest.py#L27) · [Job 入账](../../gx-h/gx_core/jobs/service.py#L689)

GX 为两种 key 分别标记 schema 版本，以规范化 JSON 计算 SHA-256。`plan_key` 标识某个 Run／图版本中逻辑节点及其展开实例、声明参数和输入绑定。`exec_key` 由实际 Handler 与 Capability 实现摘要、按角色和顺序解析的输入内容摘要、影响输出的参数、随机算法与 seed、相关模型／权重、数值精度与运行环境决定。产生测量的节点还计入 Evaluator 实现摘要。GX 在计算前展开默认值，校验类型和数值有限性。仅供放置的队列、主机提示及整图版本号留在计划记录中。更换 seed 必须产生新的 key、Attempt 和样本批次。调用端附旧 key 时拒绝请求。现有请求哈希带有资源提示，旧 path-step 快捷查找只比较路径、步骤和输入清单。P2 经 Attempt 绑定的 workflow Job 路径提交。[规范化哈希](../../gx-h/gx_core/domain/ids.py#L100) · [请求哈希](../../gx-h/gx_core/jobs/service.py#L360) · [步骤匹配](../../gx-h/gx_core/jobs/service.py#L787)

## 修订、等待与恢复

1. `amend` 携带 Run、base 版本及摘要、Decision Lease、草案摘要和幂等键。GX 核对草案字节。在同一 `BEGIN IMMEDIATE` 中复核当前会话、租约持有人、作用域、到期／撤销、Goal 版本和已批准的上限，重新计算受影响范围。先登记不可变决策 Artifact，正文引用 base 与草案摘要。新图引用该决策，再计算 `graph_sha`。新发布字节显式登记到 ArtifactService 的事务回滚清理；竞争失败后核对无可见孤立文件、事件或版本。用条件插入从该 Run 的最新行读取 head，只有 `head.version = :base AND head.graph_sha = :base_sha` 时添加 `base + 1`，要求恰好写入一行。首版要求该 Run 尚无实例图。接纳事件同时引用 `decision_ref` 与新版本，随版本、checkpoint、outbox 和幂等回执一并提交。相同已提交请求按原身份读取回执。两个不同请求争同一 base，仅一个成功，另一方读取新 head 后显式 rebase。[事务与幂等](../../gx-h/gx_core/persistence/store.py#L994) · [版本比较先例](../../gx-h/gx_core/p0/intent.py#L438)
2. 按新旧边计算变更节点及其后继闭包。等待决策只冻结该作用域，Engine 继续推进无关就绪节点。若相关输入和 `exec_key` 均匹配，可复用经验证的已完成 Artifact。对在途工作还须核对当前有效执行租约、Job 身份与准入条件，才可将原 Attempt 接入新图。无法证实归属的 Job 在原 Attempt 上对账结算。其产物若要在新图使用，需重新满足已完成产物的复用条件。[现有 Job 接管](../../gx-h/gx_core/jobs/leases.py#L409) · [产物校验](../../gx-h/gx_core/p0/manifest.py#L231)
3. 修订在同一事务追加 checkpoint，记录新图引用、分支等待、复用／接管关系，并使旧推进者的 `advance.id` 失效。跨写锁执行的节点在派发和提交 checkpoint 前均核对当前图与推进权。旧推进者即使晚到也不能覆盖新状态。在外部提交前先持久化 Attempt 和提交意图，以 `gx-node-<attempt_id>` 作为稳定调度操作名。重启按原名查询在途 Job，结果未知则留在 `OUTCOME_UNKNOWN`。计算续跑须验证 checkpoint 内容 SHA、Capability 兼容条件和父 Attempt，再创建子 Attempt 与新的操作名，保留父项结果和费用。[推进者校验](../../gx-h/gx_core/orchestration/engine.py#L1506) · [提交意图先例](../../gx-h/gx_core/p0/compute.py#L136)

## 迁移、回退与验收

新增一个由 P0 加载的 SQL 迁移。只有显式选择 Recipe 的新 Run 才写入首版，旧 Run 按其原工作流和 checkpoint 继续。缺少可验证 seed 或语义输入的历史记录仅作为原实验来源。回退时，v3.1 使用其保留的静态项目副本。动态项目保留当前库、产物及调度回执，由可识别这些 Attempt 的版本对账后再恢复。[迁移入口](../../gx-h/gx_core/p0/schema.py#L28) · [历史重复打开证据](../../acceptance/v31/final-at2-preservation.json#L502)

验收需记录同 base 竞争、撤销后迟交、seed 变化并附旧 key、仅改放置提示、下游改单独参数、一个分支等待而另一个完成，以及旧推进者和旧 Job 在新图提交后返回。核对每种情况的 GraphVersion、Attempt、Artifact 引用、实际 Job 数和结算，覆盖 A03、A05、A12–A16。
