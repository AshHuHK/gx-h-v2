# GX-H-2 源码审计

本报告检查 P0 改动前的 `gx-h-2/gx-h`。父仓提交为 `4ff89fa93b2f0f9c33fc59a45ad09ddfa17c59b7`，该目录 Git tree 为 `e631154299192ccbf32111550c223f7642161a2e`。路径和行号均相对于该目录。可以直接沿用 SQLite 事务、Job 提交与对账、预算预留与结算、不可变产物、工作流检查点、任务交付验证和双向 App Server 传输。新设计最大的差距是项目条目级变更、每次持久 Attempt 的独立 Root Thread，以及能够拒绝缺少冻结评估的正式 Evidence 写入。

| 核对范围 | 结果与证据 |
|---|---|
| 源文件 | 对 Git 树中全部 286 个跟踪文件建立 blob ID 清单，逐个重新计算工作树内容哈希，全部相符。对 211 个 Python 文件及 Python 启动脚本解析 AST。清单包含 shell 入口、TS、配置、合同、技能、示例与测试。[源文件与用例索引](gx_source_files_tests.json) |
| SQLite 定义 | 实际在空闲内存库执行原始 DDL。72 张普通表、2 张 FTS5 虚表均出现在 `sqlite_master`。另外 11 张为 SQLite/FTS 自动管理的内部表，包括 `sqlite_sequence`。项目网页消息另用一个 SQLite 文件。[主 DDL](../../gx-h-2/gx-h/contracts/db_schema.sql) |
| 生产写入 | 主库 45 张表有明确行创建 SQL。29 张没有找到生产 INSERT/REPLACE，其中 11 张有显式读取。通用状态更新器、触发器、测试种子、示例和同名 JSON 产物均已分别核对。另库 `messages` 有实际业务写入。[每表 SQL、调用方和状态](gx_source_tables.json) |
| 既有测试 | 29 个 Python `test_*.py` 文件包含 170 个测试方法定义。另有三个手工运行程序、一个空包文件，以及 TS `node:test` 的两个测试。测试定义数量与成功执行次数分别记录。[逐用例行号](gx_source_files_tests.json) |
| 证据等级 | 上述 DDL 加载是本次本地实测。下文的产品行为和测试说明来自源代码，只对引用的入口、判据和调用链负责。独立基线组在补齐环境后实跑 Python 168/170 通过、Node 2/2 通过，两个历史 SIGKILL 用例均在注入前的夹具准备阶段失败。[基线与原始日志](../BASELINE_REPORT.md) |

索引把生产、示例和测试的 `INSERT/REPLACE/UPDATE/DELETE/READ` 分栏，标明 SQL 所在的方法和行。按写入方法可继续查明确的静态 Python 调用点与函数对象引用。它还保留 17 个合同状态机的初始态、终态和每条允许边，以及各表调用 `store.transition` 的位置和目标表达式。动态路由、回调和间接测试的业务入口见下表。空测试引用表示在该层没有查到直接访问或专门行为断言，不能当作覆盖率统计。

## 正常入口与 P0 差异

| 路径 | 原有行为、调用与测试 | P0 接口或缺口 |
|---|---|---|
| `persistence/records.py` 等 Python 模块均位于 `gx_core/` 下 | `orchestration/service.py:467,522` 创建 Run，经过 `records.create_run:22` 和 Objective 行。 `teams/service.py:290` 的任务创建路径调用 `records.create_task:123`，同时保存依赖。`records.create_path:74` 有独立实现，仓内可定位的创建调用在 `examples/p01_fixture.py:53`。 `records.record_worker_observation:197` 是另一个低层写口。 | `runs.objective`、`objectives.spec_json` 按 Run 保存。团队 Intent 有自己的版本，但没有项目级 `item_id + version/hash` 或 Attempt / Job 的条目依赖。science_grid 中的 route ID 存在提案产物里，不能据此宣称实际创建了 paths 表行。 |
| `persistence/store.py:49,110,158,233,344,497,598,645,704` | 打开并迁移 SQLite，外层 `BEGIN IMMEDIATE`、内层 SAVEPOINT，提交和回滚有各自的回调。校验 run revision 和状态机后写入 transition event。状态变化的 `control_epoch` 触发器见 DDL:58。已有进程争抢和 WAL 迁移用例 `test_v2_fencing`、`test_v2_rollback`。 | 在同一连接、同一写事务中复核条目版本和受影响对象。原有 run revision 和 control epoch 无法表达逐条失效。历史检查点和产物可沿用。 |
| `persistence/store.py:835,903,919,1246,1298,1320,1341`，`orchestration/reactor.py:173,237,346,388,412` | `idempotency` 约束请求哈希/接收结果。`enqueue_outbox` 在调用方事务中查 key 并写行。Job 终态、提交记录/受理、审批等会生产消息。Reactor 按已安装 topic 加载权威行，处理后开事务标记消费、追加事件。`test_grid_job_recovery`、`test_native_facades` 包含回执丢失及回滚。 | 现有 `claim_outbox` 没有持有者租约，只读未消费行并增加次数，仓内没有正常调用它的代码。`_react_to` 在消费 CAS 之前执行业务动作。SQLite 事务、内部反应事件和 key 足以复用，向外部 Codex 投递还要有操作意图、同一对象的接收证据和未知结果对账。 |
| `orchestration/engine.py:602,620,845,891,919,1014,1353,1500,1597,1660,1934`，`workflows/science_grid/v1.json` | Engine 按有序图前进、保存节点检查点、通过 `InterruptRequest` 等待、按 `reenter` 回到节点并检查完成条件。`reactor.drive_science:188` 从现状续做，现有分支只认 `science_grid`。`test_science_cycle` 实现双轮合成检验。 | 复用检查点、阶段展开和确定性推进。项目 Event Loop 可以把新 Intent / Thread / Job 事件加入既有驱动。旧 grid 工作流是可复用的一个 fixture。 |
| `project_continuation.py:30,116,375,426,623`，`project_work.py:70,252,426` | `bin/gx-project` 使用项目设置、Run、检查点、Job 回执判断是否有新工作。等待中的健康 Job 本地休眠，恢复时再触发 `reactor.lazy/drive_science`。已结项目复用记录。`test_project_continuation` 有等待、无进展、新工作、崩溃、回滚后恢复等 8 个用例。 | 既有 `project-driver.json` 由项目主 Root 续做。`project_continuation.py:83` 用旧分派信息找到需要恢复的 worker。P0 的持久任务需要单独的 Attempt→Root 绑定。 |
| `project_messages.py:11,65,84,95,154`，`web/http.py:85,106`，`web/projects.py:87` | 本地网页将用户追加内容写到 `web-messages.sqlite3`，再通知驱动。活动 turn 对单次 steer 保留 ID、目标 thread 和 turn，从历史 `userMessage.clientId` 对账。旧批次通过固定格式的正文与已入箱的本地记录精确比对。`test_web_bridge`、`test_project_continuation` 覆盖本地反馈与重连。 | 把已发送内容认回是 delivery。主对话的真实人类输入资格、来源去重、条目 CAS、客观变更差异、后续明确人类确认需在受信入口和正式变更事务内建立。现有普通网页反馈、本地工具调用、仅有 native thread/turn 身份均不能提供这项证明。 |
| `teams/service.py:1142,1159,1262,1357,1425,1497,1670,1729,1779`，`teams/recovery.py:202,372,438`，`persistence/store.py:1018,1058,1083` | Claim 在写锁中验证 assignment、能力与尝试次数，生成包含 attempt 的 lease。提交/finish 复核当前 lease、task、状态与持有者，手稿登记和后续 validation 能独立恢复。`attempt` 与 `attempts_consumed` 区分投入工作与失败额度。相关测试包括 `test_v2_fencing`、`test_native_crash_receipts`、`test_native_facades`。 | 重用 Task、lease、提交和 attempt 号。旧设计通过 worker observation 和 host receipts 表示原生子线程做的工作。缺少独立 Root 的创建意图、Thread 与单次 Attempt 绑定、失去回执及重建规则。 |
| `teams/briefs.py:231,316,332,400,450`，`teams/brief_sections.py:59,104,200,240,291` | 任务书组合目标、上次尝试、交付、验收和策略，超长内容有完整 ArtifactRef 与摘要。任务书发布后校验 hash。`project_followup.py:287,313` 另为项目 Root 分页，`test_launch_brief_paging` 检查读取原件。 | 可把选中的 Intent 引用、预算和未完工作并入原任务书。任务书是恢复输入，权威版本仍在 GX。真实 pinned Codex compaction 后的可见性要另测。 |
| `hostobs/app_server_client.py:108,418,523,531,650,687,704`，`hostobs/native_app_server.py:1009,1043,1196,1361,1638,1660,1757,1832`，`hostobs/app_server_adapter.py:9` | 双向 stdio 客户端有消息关联、通知与服务端请求、超时后保留 pending ID、拥有进程的关闭。旧 NativeRouter 真实调用 initialize/initialized、thread/start、thread/resume、turn/start 和带 expectedTurnId 的 steer，并有 interrupt 调用。`test_app_server_client` 的 47 个协议/传输用例使用受控对端，包括真实管道。`test_native_router` 用受控路由恢复原项目 Root。 | 可直接用传输。原 router 围绕单项目 Root 及其子树。基线 GX 没有 `thread/goal/set/get/clear` 的调用链。Goal 投影、主线程 Goal 开关、无模型 Job 等待时的真实 Goal 状态以及非 OpenAI compaction 属于 pinned 上游验收。 |
| `hostobs/native_context.py:123,178,226,329`，`native_spawn.py:78,115,172,188`，`codex.py:486`，`session_receipts.py:164,376` | 有可信动态工具 callback、原生子线程创建回执和历史线程查询。另有只读访问 Codex 本地状态 SQLite 的观测路径。`test_native_spawn`、`test_native_team_subjects`、`test_native_crash_receipts` 涉及旧语义。`hostobs/source_host_adapter.py:15` 还可选择独立 source host。 | P0 接官方 App Server，长期任务通过独立 Root 执行。原生短工的子树追踪、从历史或本地 Codex DB 推断 GX Task、source host、旧 host pool 不接入新派工。原生子 Agent 的 GX 写权限应以可信 callback 的实际绑定决定。 |
| `jobs/service.py:323,390,416,508,607,680,1019,1068,1413,1453,1681,1705`，`jobs/grid_workflow.py:10,42` | Submit 拒绝外层未提交事务，先持久化请求、预算及 Job，再启动进程。同请求 key 或相同步骤复用在途工作，冲突拒绝。状态不明保留 Job 和预算再对账。终结时在事务中落结果、预算和 `job.terminal`。`test_local_job`、`test_grid_job_recovery` 使用原 Job 恢复，`test_shipped_grid` 覆盖不重复部署。 | 复用 JobService 的 admission/observe/reconcile/cancel。已校验 capability、输入角色、资源、运行状态、开关、网络和预算。还需检查当前项目可机检的 Intent Item，并将影响重算和 Job 终态的竞争放进同一 SQLite 顺序。 |
| `jobs/executor.py:150,159,190,272,334`，`jobs/runner.py:117,264,321,470,560,672,882` | 启动独立本地 runner，以进程标识、心跳、状态、终态文件与退出事件对账，支持取消、执行时间和资源上限。`test_cpu_runner` 实际启动 CPU 子进程并含拒绝二次启动/超时用例。 | 可保留现有进程资源、网络与取消 owner。测试证明本地后端的状态机，不能推导远端队列的幂等接口或 GPU 复现等级。 |
| `artifacts/service.py:365,439,475,696,879,1063,1137`，`artifacts/delivery.py:90,135,185` | 发布字节前计算 hash，不同内容不可覆盖先前同一位置。读取可重新核 hash。input manifest 记录输入引用和 hash。delivery manifest 校验特定 Run/Task/Attempt 的交付原件。`test_delivery_manifest` 有 10 个竞争、回滚、重开与孤儿回收用例。 | input manifest、交付 manifest、失败诊断 recovery manifest 的用途不同。完整 Execution Manifest 还要覆盖全部 Job 输入和结算，作为当前 Evidence 验收对象。 |
| `budgets/__init__.py:146,212,225,257,338,421,495,671,765,833,957`，`budgets/lines.py:38` | 预算 owner 已实现项目分期、额度/预留/实耗、去重结算、返还与受控上调。`JobService._budget_amounts:870`、`_measured:2092` 连通 Job。当前项目科目是 job_submissions、gpu_jobs、gpu_minutes、cpu_seconds、worker_tasks。 `test_local_job` 检查预留结算，`test_team_manager` 检查不可关闭未知 Job 的预算。 | `hostobs/native_app_server.py:1057` 收集原生 token 观测，`research/model_activity.py:239` 另计指定 profile 的调用。现有代码没有将所有 Codex token 与外部 API 用量一并纳入项目预算准入和结算。扩展同一 ledger 可保留原始费用和失去回执时的保留额。 |
| `workflows/science_grid/nodes.py:41,78,94,105,120,143,148,168,217`，`plugins/evaluator.py:17,34` | 提案冻结任意正数路线的预测、范围和阈值。每条路线提交 Job，保留原始观测。assessment 与 decision 分属两个 GX Task，validation 读 leases 中的实际身份拒绝同人。失败测量的 `supported` 必须为 null。成功支持/反驳、否定结果和 measurement failure 的行为在 `test_science_cycle.py:92,100,118,128` 有 fixture 断言。 | 工作流通过制品保存 proposal/evidence/assessment/decision，没有向同名关系表写行。evaluation plugin 仅在输入携带 `evaluator` 时运行，`nodes.py:193` 允许两侧缺失的 `evaluation_ref` 相等，成功测量只要求结论为 bool。核心需逐条要求非空、完整且适用的冻结 Evaluation。 |

### 科研结算与执行清单

- 冻结提案保留这轮的阈值和输入。`science_grid/nodes.py:101` 的 `research_evidence` 是所有路线的 Job 观察集合，含失败行。正式支持/反驳 Evidence 只能来自有效测量。关系表 `evidence` 在 DDL:651 中还支持文献主张；它没有强制指向一次具体冻结 Evaluation 输出的非空外键。P0 可在正式结算 writer 上做跨表校验，保留失败观测供复测。旧 `jobs/outcomes.py:58` 汇总技术失败类别，`teams/recovery.py:372` 管理尝试计数，目前没有四个结果分类共同产生有效 Evidence 的业务入口。 [科研校验](../../gx-h-2/gx-h/workflows/science_grid/nodes.py) [Evidence 结构](../../gx-h-2/gx-h/contracts/db_schema.sql)
- 评审独立性由 validator 读实际提交、当前/前次 lease 和协调人验证。`nodes.py:174` 排除协调人，`nodes.py:177` 排除同一 reviewer 和 decision principal。两角色使用不同模型的运行证明需靠真实执行观测。既有判断文件以 `continue_paths`、`next_grids`、`stop_reason` 表示继续、缩小或停止，后续路线必须是旧 ID 的子集。真正引入新路线的 Pivot 以及显式 Continue/Prune/Pivot/Stop 权威 Decision 记录仍需接通。 [工作流决策](../../gx-h-2/gx-h/workflows/science_grid/nodes.py) [双轮 fixture](../../gx-h-2/gx-h/tests/test_science_cycle.py)
- `jobs/service.py:607` 保存请求、capability、参数、输入 hash、可执行路径、声明的权重 hash、环境与平台源码摘要。`jobs/adapter.py:66` 对 adapter host 与其库求摘要。`jobs/runner.py:157,247,820` 采集输出、可用设备信息与退出码。JobService 结算时发布状态、输出和日志。Schema 接受不带 task 的 workflow Job，`jobs/grid_workflow.py:35` 正常使用该路径。未找到对所有 Job 必填或判定适用性的统一 manifest，能同时表示真实 Attempt/Task、实际数据版本、源码树、随机种子、依赖/镜像、运行时、后端回执、冻结评估、复现等级和已知不确定性。硬约束需要在 Job 准入前校验，并且在 Evidence 消费时重新对照当前版本。P0 如需在原生 Shell 运行前拦截，还需验证独立的 Hook 能力。 [Job admission](../../gx-h-2/gx-h/gx_core/jobs/service.py) [执行回执](../../gx-h-2/gx-h/gx_core/jobs/runner.py)
- 绑定旧冻结规格的 `planning/objective.py:330,367` 引用了该树没有的 `planning.hypotheses` 与 `gx_core.evaluation`。注册中心对缺失的能力返回 `ImplementationGap`，对应 `api.py:166`、`orchestration/seams.py:125,156`。因此文档中的状态定义、选取 evaluator 的界面、读表或没有生产者的 imported symbol 在本版本都不能算执行证明。 [Objective 绑定实现](../../gx-h-2/gx-h/gx_core/planning/objective.py) [业务接口](../../gx-h-2/gx-h/gx_core/orchestration/seams.py)

## 逐表业务核对

以下行号指向实际创建记录的写入点，完整的读、写、静态调用者、状态边和直接测试 SQL 另见逐表索引。用例栏列出可定位的相关行为检查。文件里存在函数定义但没有正常入口或专门测试时，保留这一差别。[逐表索引](gx_source_tables.json)

### 有实际写入语句的表

| 表 | 写入点与业务调用 | 状态与既有用例 |
|---|---|---|
| `schema_meta` | `persistence/store.py:65`，建库、盖版本戳、同大版本迁移由 `open_store` 进入 | 合同摘要/版本的 upsert。 `test_v2_fencing`、`test_v2_rollback` 检查迁移和保留原结果。 |
| `runs`、`objectives` | `persistence/records.py:50`，`orchestration/service.py:569`，均由 `run_start` 调用 | Run 初始 CREATED，转移走 `store.transition`。Objective 保存筛查/确认，关联自己的状态，`planning/objective.py:338` 的绑定分支另有缺失依赖。 `test_native_router`、`test_science_native_entry` 检查项目 Run；未见条目级需求用例。 |
| `coordinator_leases`、`events` | `persistence/store.py:1148,617`。项目开始、协调人接手和业务状态写入调用 | 协调 lease 随续租/交接而更新。事件带旧/新状态和实际 actor。 `test_team_manager`、`test_v2_rollback`、`test_web_bridge` 有协调人越权、恢复、事件游标断言。 |
| `paths`、`method_equivalences` | `persistence/records.py:103`，例子 `examples/p01_fixture.py:53` 创建 path；`teams/service.py:935` 可在工作链结束时改变既有 path。 `capabilities/registry.py:511` 实现方法等价记录 | Path 初始 DRAFT。 `test_p01_fixture` 经过示例，`science_grid` 自己的 route 用提案 JSON。仓内未找到等价行的产品创建调用或专门测试，且其 writer 在 `registry.py:500` 要读已存在的 `evaluations`。 |
| `tasks`、`task_deps` | `persistence/records.py:157,173`，调用者包括 `TeamService.create_task`，工作流由 `orchestration/expansion.py` 展开 | 依赖需 ACCEPTED 才能交给下一任务。`test_module_collaboration` 有独立分支返工，`test_science_cycle` 有 assessment 后从原提交启动 decision。 |
| `leases`、`submissions` | `persistence/store.py:1050` 和 `teams/service.py:2194`，从 claim、finish 和 submit 进入 | 释放或过期不能再次提交，释放时间和 attempt 保留。提交可 PENDING_VALIDATION、ACCEPTED、REJECTED、HELD。 `test_native_facades`、`test_v2_fencing`、`test_native_crash_receipts`。 |
| `worker_observations`、`host_events` | `hostobs/observation.py:289,329` 写观测/事件，调用链包括 `hostobs/native_observation.py:439` 与 `hostobs/events.py`。另有 `persistence/records.py:263`，其可查调用在 `examples/p01_fixture.py:56` | 观测按合同规则校验，逐观测记录新增一行。 `test_native_facades`、`test_native_team_subjects` 测已观测身份；这些用例不证明 P0 的独立持久 Root。 |
| `environments`、`capabilities`、`probe_records` | `capabilities/environments.py:483,555`，`capabilities/registry.py:228,572`。 `capabilities/shipped.py` 准备本地 grid；asset 转移的 `capabilities/tools.py:237` 回调可同步 capability 状态 | 环境由 UNPROBED 起，探针状态与可用 capability 关联。 `test_shipped_grid` 验证 DRAFT 不随调用自动提升和已安装能力复用。 `test_local_job` 覆盖完整 utility Job 路径。 |
| `jobs` | `jobs/service.py:680`，正常入口 `JobService.submit → _admit`；`jobs/grid_workflow.py:35` 使用同一服务 | CREATED、SUBMITTING、运行、OUTCOME_UNKNOWN、终态依照合同。终态只在持锁复核后登记。 `test_local_job`、`test_grid_job_recovery`、`test_cpu_runner`。 |
| `artifacts` | `artifacts/service.py:485`，由 publish 或 `artifacts/delivery.py:185` 注册；Jobs、任务、评审制品均复用 | 路径和 hash 对应原件。涉及不可覆盖/校验、事务提交与文件恢复。 `test_delivery_manifest`、`test_plugin_evaluator`、`test_local_job`。 |
| `dependencies` | `orchestration/expansion.py:168,171`、`governance/reviews.py:433`、`knowledge/memory.py:214` 分别记 expansion trigger、等待 review 和记忆来源 | 保存业务边，没有被引用 Intent 条目的版本/hash。 `test_module_collaboration`、`test_memory` 涉及这些所有者；没有现成的条目级变更竞争测试。 |
| `memory_docs`、`memory_fts`、`memory_fts_cjk`、`memory_queries` | `knowledge/memory.py:192,203,207,541`。MemoryService 在写入/检索时更新词、CJK 双字索引和查询记录，`knowledge/index_run.py` 有项目索引入口 | 文档有可见性和 supersede/withdraw 流程，两个 FTS 虚表随之删除或更新，查询记录追加。 `test_memory` 验证命名空间、重放和撤回。 |
| `assets`、`check_runs`、`reviews`、`adjudications` | `governance/assets.py:371`、`checks.py:144`、`reviews.py:271`、`checks.py:330`。从资产提交、检查、复核和 `governance/tools.py:636` 的 bundle validator 等入口调用 | Asset 转移、检查结果、复核判决及其 superseded 状态存在。 `test_local_job.py:30` 调用 `adjudicate` 完成本地 utility 审批条件。资产类 review 和科学 evidence_review 使用不同的表。 |
| `approvals`、`strategy_pointers` | `governance/approvals.py:443` 与旧 `persistence/store.py:1468` 均有审批插入；正常审批用 `governance/approvals.py:499` 的 decide。 `governance/pointers.py:84` 通过冲突更新 active/previous 指针 | 审批从 PENDING 经服务校验转移。strategy 可提升、还原或清除，`rsi_cycles` 本身没有创建者。 `test_local_job` 涉及审批链；未在已交付测试中找到单独验证 strategy 指针全生命周期或人类 Intent 确认的用例。 |
| `checkpoints`、`outbox`、`idempotency` | `persistence/store.py:1447,1287,1292,860`。 `Engine/StoreCheckpointer`、`JobService._end`、`TeamService`、治理和宿主事件均有调用 | checkpoint 追加，outbox 记录 consumed_at/attempts/last_error。幂等记录携带 request hash、completed_at 和 side_effect_status。 `test_grid_job_recovery`、`test_native_facades`、`test_science_cycle`。 |
| `budgets`、`budget_reservations` | `budgets/__init__.py:367,397,481` 写父/子额度、预留。`persistence/store.py:1394` 另有低层预算创建 helper。Job 与 Task 经过预算 owner | `budgets/__init__.py:671,765` 分别结算和释放。 `test_local_job` 验证终态结算，`test_team_manager` 检查未知工作保留预算。 |
| `teams`、`team_intents`、`team_grants`、`team_members` | `teams/manager.py:457,461,482,579,604`。 `teams/manager_tools.py:12` 分派给 `TeamManager.manage:417` 的 action 表 | team 状态有 CREATED/ACTIVE/PAUSED/DRAINING/CLOSED。修订后增加 team_intents 版本；成员 ACTIVE/LEFT 与领导授权代数分别管理。 `test_team_manager` 有版本、CAS、换任、撤销用例。 |
| `task_assignments`、`team_messages`、`team_message_acks` | `teams/manager.py:683,790,807`，由同一 manager 与 `teams/manager_tools.py:18` 进入 | Assignment 可 ASSIGNED/PENDING_TRANSFER，消息按关联键去重，回执限定接收方。 `test_team_manager`、`test_native_team_subjects` 有接受、转交、重复确认和范围断言。 |
| `host_pools`、`host_pool_slots`、`host_pool_inventory_receipts`、`host_pool_external_inventory` | `hostobs/pool.py:848,661,1022,430,452` 有生产写入。适配工具从 `pool.py:1987` 起，`teams/board.py:294` 在视图中读取相关容量 | slot 状态在 `pool.py:35`，转移入口在 1721。DDL 的历史注释虽称 proposal，主 DDL 实际建表且 Python 有操作实现。未找到这 4 张表的专门已交付测试。P0 独立 Root 派工不需要引入 Host Pool。 |
| 独立消息库的 `messages` | `project_messages.py:14,17,78,91,100,203` 负责建表、去重入箱、写目标、回执与对账；`web/projects.py:87` 调用 | queued → sending/steering → received/turn_completed，确定性 steer 被拒可以回 queued。 `test_web_bridge`、`test_project_continuation`。与主库间没有原子的需求变更事务。 |

### 没有生产行创建者的 29 张表

生产树、示例、测试均未找到向以下 29 张表插入数据的确切 SQL。内存库实际出现 9 个触发器，其中 8 个保护既有科研内容，第 9 个在 Run 变更时更新 control epoch。机器索引保留每张表的约束、读者和状态定义。表名与业务名相同的制品也保留原本的分类。[逐表证据](gx_source_tables.json)

| 表及 DDL 行 | 在本树找到的调用或缺口 |
|---|---|
| `eval_specs:455`、`evaluations:491` | 治理和快照读规格，能力等价判断与 Job bundle 校验读评估记录。 `governance/subjects.py:74`、`capabilities/registry.py:500`、`jobs/bundles.py:235`。 `plugins/evaluator.py:63,83` 的实际产品是锁文件与结果 ArtifactRef。 |
| `dataset_records:529` | `artifacts/tools.py:80,85` 按已登记数据或 Job 判定 held-out 输出。 |
| `sources:563`、`source_identifiers:598` | `knowledge/memory.py:162,426,433,587` 和 `governance/tools.py:456,515` 读撤回状态、来源/别名和许可。 |
| `evidence:651`、`gaps:703`、`episodes:741` | `scheduling/snapshot.py:841,883` 读取依赖和失败历史。`orchestration/service.py:904` 读取开放缺口。 |
| `rsi_cycles:954`、`imports:981`、`plugin_invocations:1320` | `governance/pointers.py:120`、`governance/tools.py:459`、`orchestration/repin.py:57` 和 `teams/service.py:626` 读取已有循环、导入或在途调用。 |
| `eval_cases:478`、`datasets:515`、`heldout_reads:544`、`source_segments:607`、`source_reads:620`、`literature_searches:635`、`evidence_reviews:686`、`memory_vectors:802`、`references_reviewed:996` | 没有显式产品行创建或 SQL 业务读取。历史 contract 文字和通用表映射提供定义。切换未安装的 evaluation 组件会触发缺失依赖。 |
| `hypotheses:1180`、`hypothesis_revisions:1188`、`research_contexts:1204`、`research_context_heads:1220`、`research_rounds:1225`、`hypothesis_update_proposals:1251`、`hypothesis_update_reviews:1275` | 主表、不可变版本、heads、轮次和复核均有定义，没有显式产品读取或写入。 `science_grid/nodes.py:55,101,153` 实际通过制品和 checkpoint 传递研究内容。 |
| `plugin_versions:1295`、`plugin_heads:1311` | 无显式业务 SQL 行读取、创建。现存评分 plugin 由文件 digest 和制品固定版本。 |

共 11 张属显式读者存在、没有行创建者，后面三组的 18 张属于既无行创建也无显式 SQL 业务读取。通用 `store.transition:704` 的表映射还能对已有行做动态 UPDATE，它不会 INSERT。 `store.py:679` 的映射涉及 16 个持久化的合同状态机，`plugin_runtime` 描述进程。 `evidence`、`eval_spec`、`source`、`gap`、`rsi_cycle`、`reference_review` 虽在映射里，基线生产代码里未找到以这些 machine 为目标的实际 transition 调用。 `objectives` 有直接写入，`team`、`approval` 和 `worker_observation` 存在各自的直接更新/观测路径。完整声明边在 [状态机索引](gx_source_tables.json) 和 [原合同](../../gx-h-2/gx-h/contracts/states.json)。

## 测试资产与结论范围

| 测试模块 | 定义数 | 源码中的验证对象 |
|---|---:|---|
| `test_app_server_client` | 47 | 双向 JSON-RPC、真实子进程 pipe、事件交错、pending ID、超时、限流、关闭，controlled peer。 |
| `test_cpu_runner`、`test_local_job`、`test_grid_job_recovery`、`test_shipped_grid` | 2 / 1 / 1 / 2 | CPU 真进程、原始 Job、保留预算、掉回执恢复及已安装 grid 复用。 |
| `test_delivery_manifest`、`test_launch_brief_paging`、`test_memory` | 10 / 1 / 1 | 原件冻结及回滚、任务书分页、作用域和撤回。 |
| `test_native_crash_receipts`、`test_native_facades`、`test_native_router`、`test_native_spawn`、`test_native_team_subjects` | 2 / 15 / 1 / 4 / 5 | 旧项目 Root/子线程协议恢复、身份/租约或政策变化；两个 SIGKILL 用例的实际基线在注入前失败。 |
| `test_project_continuation`、`test_team_manager`、`test_v2_fencing`、`test_v2_rollback`、`test_module_collaboration` | 8 / 28 / 12 / 1 / 1 | 项目续跑、团队与租约隔离、代数变更及并发/回滚、分支返工。 |
| `test_p01_fixture`、`test_p01_metrics`、`test_science_cycle`、`test_science_native_entry` | 1 / 3 / 2 / 1 | 固定夹具、计数、等待时推进/回执恢复及合成科学工作流。 `examples/science_cycle.py:96` 有 `OfflinePeer`，`bin/gx-science` 指向此 fixture。 |
| `test_model_activity`、`test_science_activity_cycle` | 7 / 1 | 旧模型活动接口的预算、掉回执、并发和与原生 decision 的衔接，其测试端点为受控对端。 |
| `test_dsh_activity`、`test_dsh_runtime`、`test_plugin_evaluator`、`test_source_host_adapter` | 2 / 2 / 2 / 2 | 可选旧 SDK、锁定插件输入与输出复用、另一路 source-host 参数。DSH 测试有环境条件。此类结果只能计入相应原组件的基线。 |
| `test_web_bridge` | 5 | 本地网页、去重投递和 SSE 事件恢复。 |
| `runtime/plugin-host/host.test.ts` | 2 | `node:test` 第 8、33 行检查 evaluator 两个 scope 并发/退场、空输入/非有限值，依赖原 Node/Cordis 环境。 |
| `tests/runtime_child_probe.py`、`tests/runtime_interrupt_probe.py`、`tests/science_runtime_comparison.py` | 手工 | 独立运行的宿主子线程、interrupt 与运行路径比较程序，不能按 Python 单元测试计数。 `tests/__init__.py` 为空。 |
| `adapters/hosts/generate.py --check`、`adapters/local/selftest/selftest.py` | 手工 | 前者比对通过源角色生成的投影，后者提供自检 capability 的输入、解析和失败分类函数。`README.md` 给出投影检查和 unittest 命令。 |

每个 Python 方法的类、名称与行号，以及 TypeScript 的完整测试标题均可查询。[用例清单](gx_source_files_tests.json)

## 复用、改造与版本隔离

1. 继续调用原 `store.transaction`、已有 Task/Job/Artifact 和 budget owner、双向 stdio 传输、science_grid 合成执行与交付校验。新增科研结算应读真实 Job / Artifact / Evaluation；评审和决策语义沿用已校验 principal 分离。
2. 在同一业务库补项目级 Intent 条目和依赖、真实人类入口、变更事务、Attempt→Root 的持久绑定以及原 outbox 的外部投递/未知结果对账。把 Job 已有事实组合成要求的执行清单，明确缺失字段、冻结评价、有效性及产物复用条件。
3. 包内 `gx_reference/schema.sql` 的 `durable_tasks`、`compute_jobs`、`outbox_events` 与 GX 已有 owner 重合。`artifacts`、`evaluations`、`evidence` 更与原表同名异构。实施时在空库与已有库两种情况下都应从同一个原业务 owner 接线。直接叠加两份 `CREATE TABLE IF NOT EXISTS` 会保留先创建的形状。
4. 基线中的旧宿主 pool、原生子线程推断绑定、DSH/Cordis、嵌入式 Rust source host 可以保留供其原入口使用。官方 App Server P0 路线无需启动这些组件。源审计没有给删除原实现的依据。

### 本地核查重放

在工作区根目录执行下列只读核查，可从 Git 对象确认版本、从原始 SQLite DDL 重新建内存库，并独立校验清单中的行创建者分组。测试的完整重放命令见[基线报告](../BASELINE_REPORT.md)。

```bash
git -C gx-h-2 rev-parse HEAD HEAD:gx-h
python - <<'PYVERIFY'
import json
import re
import sqlite3
from pathlib import Path

gx = Path("gx-h-2/gx-h")
audit = Path("gx_codex_science_harness_package/audit")
files = json.loads((audit / "gx_source_files_tests.json").read_text())
tables = json.loads((audit / "gx_source_tables.json").read_text())["sql_tables"]
db = sqlite3.connect(":memory:")
ddl = (gx / "contracts/db_schema.sql").read_text()
db.executescript(ddl)
declared = set(re.findall(r"(?m)^CREATE (?:VIRTUAL )?TABLE IF NOT EXISTS (\w+)", ddl))
assert declared == {t for t, record in tables.items() if record["database"] == "GX store"}
assert all(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() for table in declared)
writer = re.compile(r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO)\s+(\w+)", re.I)
source = [gx / f for f in files["sources"] if f.endswith(".py") and not f.startswith(("examples/", "tests/"))]
literal_created = {t for f in source for t in writer.findall(f.read_text()) if t in declared}
assert literal_created == {t for t in declared if tables[t]["row_creation_in_product"]}
print(len(declared), len(literal_created), len(declared - literal_created))
print(len(files["tests"]), sum(map(len, files["tests"].values())))
PYVERIFY
```
