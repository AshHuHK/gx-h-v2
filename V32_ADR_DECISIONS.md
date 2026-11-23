# GX-H v3.2 / P2 架构对齐与审阅交接

**建议按增量方案实施。** 保留现有 GX Engine、JobService、TeamService、预算与证据所有者。扩展两个入口：项目开始时，GX 把经选择的插件配方冻结成运行实例；项目进行中，GX 按活决策租约对实例图提交修改。Codex 可提出初始方案、修改或解决等待，也可作为普通 Task 工作者。两种身份的写入权限必须在 GX 服务端区分。

本记录适用于从 `91ea828` 创建的开发分支，只完成 Phase 0、Phase 1。以下 **existing / extend / new / deferred** 分别表示沿用、扩展、新语义和延期。新字段、模式和验收步骤都是待审设计，尚未作用于项目数据库。原始提案按 ZIP 摘要保存，以下六份是本次对齐后的审阅版本。[源码审计](V32_SOURCE_AUDIT.md) · [原包及基线核验](p2/baseline-evidence.json)

| 决策 | existing | extend | new | deferred |
|---|---|---|---|---|
| [01 · 持久执行与会话](p2/ADRs/ADR-01.md) | GX Run、Task/Lease、原生 Root、outbox | 同一 Driver 先做本地工作，按需开 Codex | 有 scope/base 的 Decision 工作、离线等待、人类接管 | 多 provider 决策调度优化 |
| [02 · 配方、节点与计算](p2/ADRs/ADR-02.md) | 包摘要、能力 JSON、工具 schema、JobService | 插件声明、注册核对、工作流解析和 GX Job 适配 | 实例化 Recipe、可观测节点、后端按 Attempt 身份对账、数值 checkpoint 合同 | 生产 Slurm/GPU 集群认证 |
| [03 · Goal 与科研验收](p2/ADRs/ADR-03.md) | Intent、预算行、Artifact、Evaluation、Evidence | 冻结 PI 原始意图，区分方法参数与验收阈值 | 有类型的 Measurement、GX 计算判决、精确证据集的 PI 签署 | 正式科学签署随实际项目及 PI 进行 |
| [04 · 图版本与执行复用](p2/ADRs/ADR-04.md) | 已安装工作流 pin、事务、checkpoint | Engine 选择 ready 分支并维护影响范围 | 不可变实例图、条件写入 head、语义键、节点 Attempt 血缘 | 全局跨项目缓存 |
| [05 · 原生提交](p2/ADRs/ADR-05.md) | v3.1 原子提交、同 Attempt 修复、原 turn 计量 | 从现有实现提取版本化协议适配边界 | 在两类 Session 各自的已验证终局入口使用该协议 | 未验证宿主或工具传输优化 |
| [06 · 角色和上下文](p2/ADRs/ADR-06.md) | 已验证的 child 关系、角色目录和有界简报 | 当前 scope/Goal/Graph 对应的服务端读取和历史摘要 | Decision 与 Task 分开提供工具和输入 | 第二套上下文、任务或身份存储均无需求 |

## 沿用户目标走一遍

1. **Human Goal → 第一次 Codex Decision Session。** 人类通过既有 operator 入口明确目标、验收标准和支出上限；GX 保存原文或结构化原件、Goal 版本与摘要。现有 Run 启动要求先有已安装工作流。为新入口注册一个固定摘要的 GX 配方宿主工作流，由同一 Engine 启动；其首节点只保存初始决策 Task、checkpoint 与 outbox。Run、授权与资金先落地，此时允许无实例图、无选定 Recipe、无 Codex。使用原 run key 恢复引导，以空 base 建立初始决策工作，首版 CAS 完成后才派发配方节点。范围、有效预算和插件目录组成输入，模型只提出 recipe、参数和理由。取用旧的 human receipt、读到 Goal、或在 Task 中写出相同文本均不能授权加预算。
2. **Recipe → GraphVersion。** GX 先解析经安装批准的包及依赖，再验证声明、展开默认值，绑定人类 Goal、资源限制、seed、节点/工具/能力/Evaluator 摘要、边、monitor、停止条件和决策来源。默认值缺失、歧义、图循环或引用未解析时不产生 head。把规范化 payload 写为不可变 Artifact 并记录首版。旧项目仍按已安装图及其原摘要恢复。
3. **GraphVersion → Engine → 计算。** 同一个 Engine 找出依赖已满足且未冻结的节点。可见的 FTCS/ADI 两支各自提交 Job；先持久化计算 Attempt、输入 SHA、操作键、稳定后端名字及 outbox；JobService 随后在自己的准入事务建立唯一 Job、预留并冻结执行 manifest，提交后才联系后端。等待外部回执时其他可运行节点继续。现有 Job 服务记录输入、输出、费用与终态。
4. **计算 → Evidence / Measurement。** 归档原始场与计算条件。独立数值检验可从原件重算；插件输出度量值、单位、语义类型与有效性。GX 校验来源、比较冻结 Goal，保存计算判决和 evidence。形成科学意义超出机械判据的结论时，保持 PI 待签。模拟器接受一次请求本身没有产生数值证据。
5. **第二次 Decision Session → Amendment → Resume。** 只有已声明并命中的 monitor 触发有 scope 的决策工作；条件与证据、当前 head、有效 Goal 和历史费用固定在输入里。模型提交精确的 `base_version + lease_id + scope + draft_digest + idempotency_key`。GX 在同一事务内复核权限与预算，在正确 base 上提交下一版并唤醒同一 Run。仅影响闭包内语义改变的节点需要重算。迟到失权结果仍保留原 Job 费用和历史产物，只能经有效规则进入当前图。
6. **关闭 Codex 后的判据。** 保持 GX 进程和后端存活，禁用 Codex 自动拉起。在 GX 已有决策、且还有待运行的独立节点时，关闭全部模型宿主。观察该节点在关闭之后真正推进，记录新的 Job 结果、Artifact 和 checkpoint；待决策分支保持等待。恢复服务后只为仍有效的 scope 请求决策。另作独立故障：后端已接受而 GX 未持久化句柄时终止 GX，通过同一后端身份找回原 Job，一次执行、一次计费。两个故障点分别判定。[分项条件和证据标签](p2/ACCEPTANCE_MATRIX.md)

## 共享事务和最小数据变更

建议只新增两类持久关系，沿用 `p0/schema.py` 的增量迁移入口。版本号、操作与判决在原事务、原事件和 Artifact 所有者中落地。设计字段名用于对齐，DDL 留在 Phase 2。

| 拟新增关系 | 每行代表什么 | 已有关系不足的原因 |
|---|---|---|
| `graph_versions` | Run 的一个不可变实例版本，含单调版本、同 Run 父版、graph Artifact ref/SHA 和决策正文引用。接纳事件引用版本与决策。最新已提交版本为 head。 | `runs.workflow_sha256` 负责安装代码 pin；原 checkpoint 保存执行进度；原 science_decisions 对应一轮 grid 研究，均不表达实例版本链。 |
| `node_attempts` | 一次具有独立执行语义的节点或显式 fan-out 单元，含 graph 来源、逻辑节点、plan/exec key、原始输入/实现/seed、可选 task/lease、parent/checkpoint 和可对账的操作身份。 | Task 可多次领用且每次可发多个 Job。确定性工作流节点未必有 worker Root；Job manifest 不是所有节点的原始执行身份。旧 ID 保持原意。 |

**Graph 提交的唯一事务由 GX 持有。** 用原 SQLite 写事务和幂等 owner，先鉴别来自哪个 session，再查同一请求回执；合法的相同请求重放返回已有结果，同键不同摘要拒绝。对于新请求，在事务内核对 live lease、scope、base 版本及 SHA、当前 Goal 与资源策略，执行 head 的条件插入并检查行数。决策正文复用现有不可变 Artifact（可引用模型已经提交的原件），其摘要不引用尚未生成的新图摘要；新图绑定该决策 ref，接纳事件在同一事务引用决策 ref 和新图 ref。新发布的决策及图文件须显式登记到现有事务回滚清理；输掉 CAS 的一方不留下可见 Artifact、head 或待处理通知。发生进程崩溃时另核对磁盘残件和已提交引用。一并保存受影响/可复用映射、唤醒和 receipt。并发的异键同 base 最多成功一个，另一方获得当前 head 以便 rebase。原协调器续租、节点完成、写入一般事件都可能推进 run revision，不能把它单独当作 graph head。回调不进行嵌套宿主读取。[原事务](gx-h/gx_core/persistence/store.py#L502) · [原幂等路径](gx-h/gx_core/persistence/store.py#L994) · [Artifact 回滚钩子](gx-h/gx_core/artifacts/service.py#L503)

**Job 对账由既有 JobService 持有。** 事务一由 GX 建立或重放节点 Attempt 和 keyed outbox，此时尚未因该项 Job 准入扣费。消费者在事务一结束后进入 JobService；其准入事务二重新核对该 Attempt 的图归属、当前执行权、操作身份和资源上限，建立或找回同一个 Job、预留及 manifest。先提交事务二，再用现有状态机将该 Job 从 CREATED 条件推进到 SUBMITTING 后投递。复用既有 owner 时需扩展到动态图 Attempt；原生 Task proposal 的 reserved ID 校验目前要求 Root，不能伪造 Root 或把“已有 Job 行”视为后端已接受。[原校验与 drain](gx-h/gx_core/p0/compute.py#L144) · [原 Job 提交](gx-h/gx_core/jobs/service.py#L1037)

逻辑身份 `plan_key`、科学复用键 `exec_key` 和投递某次 Attempt 的稳定 operation ID 用途不同。新 seed 产生新 Attempt；同一 Attempt 的网络重试沿用同一后端名字。后端延迟可见、匹配多个或查询失败时保留 `OUTCOME_UNKNOWN` 及预留；不能把一次“找不到”变成二次提交许可。求解器续算须先确认父执行已终止或后端已撤销旧作业的执行权；父 Job 仍可能运行时先对账。再核验 GX 已登记的 checkpoint 内容 SHA、生产者、网格/格式和方法兼容性，创建带 parent 的新 Attempt。原 Attempt 及原结算保留。

**等待和采用原结果由同一 Engine 决定。** 记录分支待决策状态可以早于模型启动。按旧、新两张图的依赖传播影响，在现有 checkpoint 保存分支状态和复用引用。正在运行的尝试保留原版归属；只有语义键一致、原执行权仍有效且当前输入仍适用才可采用。含糊的外部副作用先对账。Codex 停机和系统主动等待都不能消耗执行失败预算。这里不需要新增 scheduler、事件总线、预算、证据仓库或双份会话账本。

## 迁移与回退决定

- 新增独立的 P2 SQL 迁移，只在可写打开时按摘要安装。历史已应用 SQL、冻结的合同内容、`science_grid@2/@3` 摘要、既有 Job 和历史计量均保持其原语义。新能力字段使用显式版本的扩展，防止修改旧合同引起全量摘要变化。旧核心合同行表在重新同步时会检查多余列；graph 与 Job 的关联存在新关系及已有 manifest/binding JSON 中，不向旧 jobs/runs 偷加列。[核心合同复核](gx-h/gx_core/persistence/store.py#L174)现有 Run 不补造 GraphVersion、节点 Attempt 或 PI 签名。[迁移入口](gx-h/gx_core/p0/schema.py#L28) · [既有历史逐表核验](acceptance/v31/final-at2-preservation.json)
- 在 Phase 2 用历史数据库副本测试两次可写打开、原列/外键/旧 run pin 和原项目冷恢复。新项目需显式选择 recipe 入口。失去精确 plugin/handler/evaluator pin 时仅暂停依赖它的工作，缺件状态保留到可恢复。
- 回退时先停止 P2 新准入，让已接受的外部 Job 对账并保留原费用、操作名、产物和侧车。静态项目可在迁移前一致备份及匹配 v3.1 发行物上恢复；已建动态图的 Run 保留 P2 数据和匹配 reader/driver，未决 Job 继续由其原 owner 对账。恢复旧 DB 快照不能撤销外部计算。没有把带新状态的项目直接交给旧版运行的承诺。[已有回退实践](V21_ROLLBACK_REPORT.md#L18)

## 下一批的顺序与退出条件

**Phase 2** 按权限与 Goal → 图与 CAS → Attempt/Job 故障恢复 → 分支等待/重用 → v3.1 回归的顺序验证。使用普通 SQLite、确定性 driver 以及可故障注入的本地 scheduler；协议专项可用官方宿主和环回脚本服务，不消耗付费科研模型 token。另在初始 Run 已建而决策尚未派发、首次图已提交而未开始派发两个窗口重启，验证一个 Run、一份首版图及唯一的有效工作。只有在并发用例使用独立连接、崩溃用例使用不同进程，并能以 Job 和结算回执证明结果时，才签相应条目。

**实施包样例与现有 API 的适配。** 配方模板的 `id/type/edges` 需规范化为同一个 Engine 的节点与依赖；原插件能力注册支持包内 JSON，示例 YAML 需换成受支持格式。示意代码的 `ctx.jobs.request`、`ctx.execution` 和 `ctx.plugin` 均非现行接口。节点通过 GX 窄接口提交已声明的意图，Evaluator 从原始数值场独立计算误差。把唯一 monitor 放在双支 join 之后无法检验单支等待时的推进，须加一个可以在 join 前触发的分支事件。[现有插件接口](gx-h/gx_core/plugins/author_api.py#L128) · [原生图格式](gx-h/gx_core/orchestration/loader.py#L37)

**Phase 3** 使用二维热传导 FTCS/ADI 独立插件做实际 CPU 计算。把原生真实模型决策、官方宿主环回、模拟 scheduler、真实数值计算和独立审阅分开标记。真实 Decision Session 的模型端到端记录只在后续有明确可执行预算的该次验收中取得。插件至少具有包内 Recipe、自有节点、独立新能力、工具 schema、Evaluator 和分支 monitor；不依赖 grid 的注册/导入和业务逻辑。原包所列 **Phase 4** 是后续整体回归、性能和交付签收。当前停在 Phase 1 交审，22 项 P2 用例均未执行。[A01–A22 及对抗用例](p2/ACCEPTANCE_MATRIX.md)
