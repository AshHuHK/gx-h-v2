# GX-H v3.2 / P2 架构决策

**Phase 1.5 决定先验证 GX 独立推进已安装的静态 Workflow。** GX Engine、Driver、JobService、TeamService、预算和证据各沿用现有所有者。Codex 仅在需要决策或执行 Agent Task 时接入。PI 持有 Goal 和硬预算的授权。

分支 `v3.2-p2` 的本轮基线为 `51e0e13583d95e9cad1386e1d4c77324977e97ea`。前轮的 `91ea828` 是 v3.1 源码审计基线。下表区分 2A 的静态执行与其后的契约，文档决策自身不构成运行证据。[本轮交付包](GX_H_Phase1_5_Phase2A_Package.zip) · [11 项增补](V32_PHASE_1_5_DELTA.md) · [2A 源码路径](PHASE_2A_SOURCE_MAP.md) · [前轮审计](V32_SOURCE_AUDIT.md)

| 决策 | 既有所有者与 2A | 后续设计 |
|---|---|---|
| [01 · 持久执行与会话](p2/ADRs/ADR-01.md) | 静态节点按依赖推进，Driver 先对账和结算。宿主缺席时等待限于相应 scope。 | 2B 在原 Task/Lease 上实现 Decision Lease 的签发、续期、撤销与提交复核。 |
| [02 · 配方、节点与计算](p2/ADRs/ADR-02.md) | JobService 对已提交 Job 按原身份查询。 | 2C 定义后端能力、三种身份与未知结果处置。未来第三方普通节点在有界 Worker Process 执行，重计算交既有 Job Runtime。 |
| [03 · Goal 与科研验收](p2/ADRs/ADR-03.md) | 沿用 Intent、预算、Artifact、Evaluation 和 Evidence。 | 首版实例图前持久化 PI 的输入和授权。Evaluator 返回带来源的 Measurement，GX 按 Goal 判定。 |
| [04 · 图版本与执行复用](p2/ADRs/ADR-04.md) | 使用已有静态工作流 pin、checkpoint 和 outbox。 | 每个 Run 一个图头，以 CAS 提交。SQLite Writer 复核可变条件，图、文件和摘要的准备在锁外完成。 |
| [05 · 原生提交](p2/ADRs/ADR-05.md) | 回归 v3.1 原子终态提交、原 Attempt 修复、原 turn ACK 与计量。 | 2D 由会话协议和能力声明选择终态及 Product/Evidence 交付路径。 |
| [06 · 角色和上下文](p2/ADRs/ADR-06.md) | 回归 native child 作用域、Role Tool Catalog、Brief 与 Artifact 预载。 | 2D 由分配的职责、获准引用及插件声明选取工具和输入，通用路径独立于 Grid 工作流 ID。 |

## 研究项目中的决策与恢复

1. **首次决策。** 首版 GraphVersion 之前，GX 须能从持久记录恢复同一 Run 的 Goal（含验收条件）、硬预算、精确的插件组合及初始化授权。优先复用既有 Run、Intent、预算、组合回执和 operator 记录，沿原 run key 恢复。此时可以没有选定 Recipe、图或在线 Codex。固定引导 Workflow 和新增 Run 状态机均非前置条件。后续建图时以空 base 提案，首版单头 CAS 成功后再派发配方节点。[会话与引导](p2/ADRs/ADR-01.md) · [Goal 与授权](p2/ADRs/ADR-03.md)
2. **Recipe → GraphVersion。** GX 先解析经安装批准的包及依赖，再验证声明、展开默认值，绑定人类 Goal、资源限制、seed、节点/工具/能力/Evaluator 摘要、边、monitor、停止条件和决策来源。默认值缺失、歧义、图循环或引用未解析时不产生 head。把规范化 payload 写为不可变 Artifact 并记录首版。旧项目仍按已安装图及其原摘要恢复。
3. **Engine 与计算。** 同一 Engine 挑选依赖已满足的节点。未来示例的 FTCS/ADI 可各自提交 Job。GX 先持久化计算 Attempt、输入摘要和外部提交意图，再交 JobService 准入、预留并冻结 manifest。后端返回的调度句柄随后关联到原操作，其他分支可继续推进。[计算身份与查询](p2/ADRs/ADR-02.md)
4. **计算 → Evidence / Measurement。** 归档原始场与计算条件。独立数值检验可从原件重算；插件输出度量值、单位、语义类型与有效性。GX 校验来源、比较冻结 Goal，保存计算判决和 evidence。形成科学意义超出机械判据的结论时，保持 PI 待签。模拟器接受一次请求本身没有产生数值证据。
5. **第二次 Decision Session → Amendment → Resume。** 只有已声明并命中的 monitor 触发有 scope 的决策工作；条件与证据、当前 head、有效 Goal 和历史费用固定在输入里。模型提交精确的 `base_version + lease_id + scope + draft_digest + idempotency_key`。GX 在同一事务内复核权限与预算，在正确 base 上提交下一版并唤醒同一 Run。仅影响闭包内语义改变的节点需要重算。迟到失权结果仍保留原 Job 费用和历史产物，只能经有效规则进入当前图。
6. **关闭 Codex 后的判据。** 2A 保持 GX 和现有 Job 后端可用，停用自动拉起。在同一 Run 中令 A 等待决策、B 的已提交 Job 完成，核对 GX 是否在宿主缺席期间对账并完成 B 的确定性后继，A 和其后继仍等待。另测 A 等决策、B 等 Job、C 已就绪，及杀停 GX 后在同一库接续。远端接收后丢失调度句柄的对账试验归入 2C。[2A 源码路径](PHASE_2A_SOURCE_MAP.md) · [P2 长期验收](p2/ACCEPTANCE_MATRIX.md)

## 后续动态图的数据与事务

后续图版本及执行语义需要以下两类持久关系。2A 用既有 Run、Workflow、Task/Lease、Job、checkpoint 和 outbox 验证持久执行，无需这两张表。

| 拟新增关系 | 每行代表什么 | 已有关系不足的原因 |
|---|---|---|
| `graph_versions` | Run 的一个不可变实例版本，含单调版本、同 Run 父版、graph Artifact ref/SHA 和决策正文引用。接纳事件引用版本与决策。最新已提交版本为 head。 | `runs.workflow_sha256` 负责安装代码 pin；原 checkpoint 保存执行进度；原 science_decisions 对应一轮 grid 研究，均不表达实例版本链。 |
| `node_attempts` | 一次具有独立执行语义的节点或显式 fan-out 单元，含 graph 来源、逻辑节点、plan/exec key、原始输入/实现/seed、可选 task/lease、parent/checkpoint 和可对账的操作身份。 | Task 可多次领用且每次可发多个 Job。确定性工作流节点未必有 worker Root；Job manifest 不是所有节点的原始执行身份。旧 ID 保持原意。 |

**Graph 提交。** 在固定的 base、Goal 和输入快照上，先在 Writer 外完成 schema/图规范化、依赖与影响闭包、大文件解析与写入、摘要计算。决策正文只引用草案和 base，预分配的决策 ref 可用于构建新图。用 ArtifactService 准备并持久化不可变字节。只有在写事务内复核当前 Run/分支状态、head 版本及 SHA、lease、session/授权、Goal、预算、幂等键及请求摘要后，才按条件登记 Artifact 引用、单头版本、checkpoint、receipt、事件和 outbox。预计算依据已变即退出，读取新快照再提案。竞争失败不能发布业务可见引用，清理仅限未被采纳的私有暂存。相同已提交请求返回原回执。异键同 base 只接纳一个，其余显式 rebase。后续记录 CAS 冲突率、重提次数、额外 token/耗时和真实 scope 重叠。Run 通用 revision 会随日常事件变化，图 CAS 使用本身的 head。[图版本与事务](p2/ADRs/ADR-04.md) · [已有事务与幂等](gx-h/gx_core/persistence/store.py#L994) · [Artifact 登记](gx-h/gx_core/artifacts/service.py#L475)

**Job 对账由既有 JobService 持有。** 事务一由 GX 建立或重放节点 Attempt 和 keyed outbox，此时尚未因该项 Job 准入扣费。消费者在事务一结束后进入 JobService；其准入事务二重新核对该 Attempt 的图归属、当前执行权、操作身份和资源上限，建立或找回同一个 Job、预留及 manifest。先提交事务二，再用现有状态机将该 Job 从 CREATED 条件推进到 SUBMITTING 后投递。复用既有 owner 时需扩展到动态图 Attempt；原生 Task proposal 的 reserved ID 校验目前要求 Root，不能伪造 Root 或把“已有 Job 行”视为后端已接受。[原校验与 drain](gx-h/gx_core/p0/compute.py#L144) · [原 Job 提交](gx-h/gx_core/jobs/service.py#L1037)

`attempt_id` 标识 GX 节点执行，`operation_id` 标识一次外部提交操作（重试沿用），`scheduler_job_id` 是后端受理后提供的句柄。`plan_key` 和 `exec_key` 分别服务于计划记录与科学复用。2C 按后端声明的幂等、查询、权威未提交证明、取消和 checkpoint 能力对账。有歧义时保留 `OUTCOME_UNKNOWN`、原预留和证据，达到策略时限后交 operator 查证。需要另起计算时核实原执行状态和 checkpoint 来源，并保留父项结算。[操作员处置](p2/ADRs/ADR-02.md)

**等待和采用原结果由同一 Engine 决定。** 记录分支待决策状态可以早于模型启动。按旧、新两张图的依赖传播影响，在现有 checkpoint 保存分支状态和复用引用。正在运行的尝试保留原版归属；只有语义键一致、原执行权仍有效且当前输入仍适用才可采用。含糊的外部副作用先对账。Codex 停机和系统主动等待都不能消耗执行失败预算。这里不需要新增 scheduler、事件总线、预算、证据仓库或双份会话账本。

## 迁移与回退决定

- 后续两类持久关系沿用独立的 P0 增量迁移。已有 SQL、合同行、Grid pin、原 Job 和历史计量保留原语义。旧项目不补造 GraphVersion、节点 Attempt 或签署。新增字段随版本化声明或 manifest 提供。[核心合同复核](gx-h/gx_core/persistence/store.py#L174) · [迁移入口](gx-h/gx_core/p0/schema.py#L28) · [历史逐表核验](acceptance/v31/final-at2-preservation.json)
- 2A 用历史数据库副本核对原 run pin 和冷恢复。待后续引入数据迁移时，再验证两次可写打开、原列/外键和显式选择 Recipe 的新入口。缺少精确 plugin/handler/evaluator pin 时仅暂停依赖它的工作。
- 回退时先停止 P2 新准入，让已接受的外部 Job 对账并保留原费用、操作名、产物和侧车。静态项目可在迁移前一致备份及匹配 v3.1 发行物上恢复；已建动态图的 Run 保留 P2 数据和匹配 reader/driver，未决 Job 继续由其原 owner 对账。恢复旧 DB 快照不能撤销外部计算。没有把带新状态的项目直接交给旧版运行的承诺。[已有回退实践](V21_ROLLBACK_REPORT.md#L18)

## 分期与验收

**2A** 仅调整静态 Engine/Driver 在原 Run 中选择就绪节点、消费持久事件、对账已提交 Job、推进确定性产物和结算、按需联系 Codex 的顺序。复用既有待结用量、退避与恢复路径。验收包括 A01、A14、跨进程重启、空闲唤醒、暂时无法查询 Job 和 v3.1 回归。用 SCRIPTED 或 OFFICIAL_HOST_LOOPBACK 记录相应证据。[2A 源码路径](PHASE_2A_SOURCE_MAP.md)

**2B** 负责动态 GraphVersion/Amendment、严格单头 CAS 和完整 Decision Lease。**2C** 负责外部 Job 的提交身份、后端能力、未知结果处置及 checkpoint 续算。**2D** 泛化 Terminal Submission、Task Brief/Artifact Preloading、Role Tool Catalog 和 Product/Evidence 交付，保留 Grid 的历史兼容。普通第三方节点的有界 Worker Process 留待插件执行阶段。生产 Slurm 仍需独立后端验收。[分期与 11 项决策](V32_PHASE_1_5_DELTA.md)

**Phase 3** 的独立科学插件和 Research Intelligence 属于后续工作。原 FTCS/ADI 案例需要包内 Recipe、节点、独立能力、Evaluator、分支 monitor 和真实数值证据。模板的节点与依赖须映射到现有 Engine。早于 join 的分支事件用于证明另一分支仍能推进。原 A01–A22 矩阵继续作为整个 P2 的验收计划。[原验收矩阵](p2/ACCEPTANCE_MATRIX.md) · [现有插件接口](gx-h/gx_core/plugins/author_api.py#L128) · [原生图格式](gx-h/gx_core/orchestration/loader.py#L37)
