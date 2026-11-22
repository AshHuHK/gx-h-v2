# GX-H-v2.1 — Durable Multi-Workflow, Multi-Team Scientific Harness
## 修订架构蓝图 v2.1

**定位：** Durable Multi-Agent Execution Harness  
**架构组合：** GX Control Plane + Official Codex App Server + Scientific Job Runtime

> 基线：本次静态审查对应 `AshHuHK/gx-h-v2` HEAD `2314eed72822387e03378e8e676ad5fa9228a8d0`。实施前必须重新读取最新 HEAD，核对所有代码路径和协议行为。本文是目标设计，不代表功能已经实现或测试通过。

## 0. 修订结论

本版采纳审查提出的核心修正：

1. P0 Service API 提前到第一阶段，先移除 P0 对 v1 TeamService 私有方法的依赖。
2. Token 缺失回执不永久等待人工；采用自动估计结算和后续账本调整。
3. Thread 累计 Token 是总量主账，Turn 级精确分摊是辅助账；不因单轮归属不明永久阻塞同一 Thread。
4. Team 仅作为 Task 标签、子预算和并发上限，不建设 Team Manager、Team Scheduler、常驻 Lead Agent。
5. Workflow Registry 已存在，重点增加行为挂点，而不是只增加声明式 Schema。
6. 保留单一协调循环和 SQLite 单写者语义；只把真正阻塞的计算移入 Job。
7. 不自建 RPM/TPM 限流器，消费 Codex rate-limit 更新。
8. 复用现有 Job/Capability 工具；只增加一个通用受控 Artifact Analysis Job。
9. 不提前建设复杂优先级、四种并发 Profile 或大型监控系统。
10. 不建设 Project Registry；一个状态目录仍对应一个 Project Workspace，Workspace 内支持多个 Run。

---

# 1. 目标与非目标

## 1.1 目标

- 一个 Project Workspace 内允许多个独立 Run 并行，每个 Run 固定 Workflow ID + Version。
- 每个 Run 可有多个 Team 标签；Team 只承担任务归属、子预算和并发上限。
- 每个 Durable Task/Attempt 绑定独立 Codex Root Thread。
- Root 内短期并行继续使用 Codex Native Subagents。
- GX 控制 Root、Team、Provider 和计算资源准入。
- Agent 能发现已注册 Capability，并提出参数化计算 Job。
- Agent 可提交受控分析脚本，在无网络隔离的 CPU Job 中分析指定 Artifact。
- Job 与 Agent Turn 生命周期分离；等待 Job 不持续占用模型执行容量。
- 活动 Lease 续期；失效 Attempt 由 Fencing 阻止提交。
- Token 采用 Thread 累计账本、估计结算和后续 adjustment。
- 软预算、硬预算、在途最大风险暴露分开管理。
- 预算耗尽后仍能通过不依赖付费模型的 Operator Control Plane 管理项目。
- Workflow 行为由可信 Workflow Package 提供 Hook，核心不直接 import 具体科研 Workflow。
- 逐步建立精简的 P0 Runtime Dependency Closure。

## 1.2 非目标

- 不修改/Fork Codex，不重写 Agent Loop、Subagents、Thread、Session、Compaction。
- 不引入 DSH/Cordis、Temporal、LangGraph、Prefect。
- 不建设 Plugin Host。
- 不建设 Team Agent Manager、Team Scheduler、常驻 Lead Agent。
- 不建设 Project Registry 或多租户服务。
- 不建设完整通用 DAG 平台。
- 不开放无边界 Shell、网络或宿主机文件访问。
- 不自建 RPM/TPM 限流器。
- 不实现完整 Research Intelligence / MCTS / Bayesian Optimization。
- 不一次性重写或删除全部 v1 代码。

---

# 2. v2 现有能力复用地图

实施 Agent 必须重新核实最新代码、调用方和事务边界。

| 现有路径 | 已有能力 | v2.1 处理 |
|---|---|---|
| `p0/driver.py` | 单一协调循环、消息、Outbox、派发、poll | 保留单循环，减少阻塞工作 |
| `p0/roots.py` | Root 创建、身份绑定、工具桥 | 复用，抽公共 Service API |
| `p0/root_delivery.py` | Turn 投递、Token 通知、累计用量 | 改造计量账本，移除 rollout 文件硬依赖 |
| `p0/waits.py` | Job wait 与 Lease 保护 | 复用并接入活动续租 |
| `jobs/leases.py` | `renew`、Lease 有效性 | P0 正式调用 |
| `jobs/tools.py` | `gx_job_submit/status/cancel` | 接入现有工具，不重复实现 |
| `capabilities/tools.py` | `gx_capability_inspect` | 接入现有工具 |
| `capabilities/manifest.py` / `registry.py` | Capability 契约与注册 | 复用 |
| `orchestration/service.py` | Workflow Registry / `default_registry` | 复用并扩展行为挂点 |
| `budgets.open_child` | 父子预算 | Team 子预算直接复用 |
| `contracts/db_schema.sql` | 已有 Team 相关表 | 不启用旧 TeamManager 全语义 |
| `p0/science.py` | Evaluation、Evidence、结算 | 保留 |
| `runtime/plugin-host/` | 可选 Node/Cordis evaluator host | 不纳入 v2.1 核心 |

不得新建同类 Service，除非 ADR 证明现有实现不能满足。

---

# 3. 总体运行架构

```text
Human / CLI / API
        |
Project Workspace (one state directory / one process)
        |
        +---- Shared Intent / Project Budget
        |
        +---- Run A ---------------- Run B
        |       |                      |
        |    Workflow A             Workflow B
        |       |                      |
        |    Team tags               Team tags
        |       |                      |
        |    Tasks                   Tasks
        |       |
        |    Attempt / Lease / Fencing
        |       |
        |    Codex Root Thread
        |       |
        |    Native Subagents (Codex-owned)
        |       |
        +-------+-------------------------+
                |
          GX Tool Gateway
                |
       Capability Discovery / Job Proposal
                |
         GX Admission + Budget
                |
          Existing Job Manager
                |
       Local CPU / GPU / Remote Runtime
                |
       Artifact + Execution Manifest
                |
       Evaluator / Evidence / Decision
```

Project = 状态目录和顶层 Intent/Budget 边界。  
Run = 一次 Workflow Execution，固定 Workflow ID + Version。  
一个 Workspace 可以有多个并行 Run，不新增全局 Project Registry。

---

# 4. Token 计量与预算

## 4.1 单位必须分开

- Provider Usage：供应商回报的 Token 统计。
- GX Budget Units：GX 用于准入的 Token 预算单位。
- Provider Money Cost：按模型、缓存类型和供应商价格计算的货币成本。

不得把 Token 数直接称为美元费用。若沿用 Codex `totalTokens`，必须明确其类别口径；当前审查指出 cached input 也计入 `totalTokens`，不得重复计数。

## 4.2 Thread 累计总量为主账

每个 Thread 保存：
- latest observed cumulative total；
- observation timestamp/source；
- last reconciled cumulative total；
- unresolved delta；
- confidence；
- pricing reference（若计算货币成本）。

累计总量应单调核对。若出现下降或不一致，记录协议异常，不静默归零。

Turn 分摊：
- 有可靠前后累计快照时计算增量；
- 缺少单轮回执但后续 Thread 累计值继续增长时，优先保证 Thread 总量账正确；
- 无法精确归属的增量记为 `UNATTRIBUTED_THREAD_USAGE`，不伪造精确 Turn 用量。

## 4.3 自动估计结算

终态 Turn 超过 reconciliation grace period 仍缺最终回执：
1. 记录已观测用量下界和原始预留。
2. 自动生成估计结算，覆盖已观测下界及预留所代表的风险。
3. 标记 `ESTIMATED`，不得伪装为 `PROVIDER_CONFIRMED`。
4. 后续可信累计数据到达时追加 adjustment ledger entry。
5. 不重复计费，不把估计值和真实值双重相加。

状态示例：
`RESERVED → OBSERVED → ESTIMATED_SETTLED → PROVIDER_RECONCILED`
或 `RESERVED → PROVIDER_CONFIRMED`。

正常流程不得永久要求人工结算。

## 4.4 同一 Thread 可以继续

原则上不因某个 Turn 的分摊不明永久锁死 Thread：
- 新 Turn 按最新累计总量和未解释风险重新预留；
- 不把旧增量错误归属到新 Turn；
- 只有 Thread 总量风险本身无法安全估计时，才暂缓该 Thread 的付费执行；其他独立 Root 可继续。

## 4.5 软预算、硬预算

Soft Budget：
- 达阈值后停止低优先级新 Turn / 新 Job；
- 已准入工作按安全策略结束；
- Operator 可调整授权预算。

Hard Budget：
- 新 Turn 入场前预留；
- 对累计 `totalTokens` 更新进行实时比较；
- 触及硬上限时请求 `turn/interrupt`；
- 计算多个在途 Root 的最大未观测暴露；
- 记录中断请求、实际终态和账本调整。

必须诚实说明通知延迟：硬预算是有界风险控制，不是绝对零超支。用单 Turn 最大预留、最大并发和通知滞后定义最坏暴露上界。

## 4.6 Operator Control Plane

不依赖付费模型 Turn 的本地 CLI/API：
- status / inspect
- pause / resume
- cancel
- budget preview / apply
- usage reconciliation inspect
- pending message inspect
- retry / recover
- export audit report

所有操作经过 GX 权限验证并留下审计事件。

## 4.7 去除 rollout 文件硬依赖

正常计量使用官方 App Server 协议通知与持久化回执。rollout 文件仅可选用于诊断，不作为启动和正常计量硬依赖。

候选 Codex 版本必须通过协议探针验证 Token Usage、Turn Terminal、Native Child、Rate Limit、Resume、Steer、Interrupt 后才能加入支持范围。

---

# 5. Lease Heartbeat 与长程任务

复用 `jobs/leases.py` 的 `renew`，不新建 Lease 系统。

续租时核对：
- Task ID / Attempt / Lease ID；
- Principal / Root Thread；
- 当前 Task 状态；
- 当前 Intent/control epoch；
- Run 是否暂停、取消或终态。

过期且失效的 Lease 不得复活。

区分：
- `AGENT_ACTIVE`：可验证活动 Turn，按心跳续租；
- `JOB_WAITING`：Agent Turn 已结束，Job 继续执行，使用现有 wait protection；
- `PROVIDER_BACKOFF`：等待 rate-limit reset，不算 Agent failure；
- `BUDGET_WAITING`：等待预算，不算 Agent failure；
- `USER_PAUSED`：等待人类恢复；
- `RECOVERY_PENDING`：等待协议/账本对账。

---

# 6. Driver 与事件循环

保留单一协调循环和 SQLite 单写者边界，不拆成七个 Worker。

只移出真正阻塞工作：
- App Server 回调只做轻量验证、记录和排队；
- 行内数值计算转 GX Job；
- 不在回调里嵌套 App Server RPC；
- 不让 CPU/GPU 计算阻塞事件消费；
- 状态变更和 Outbox 仍按现有事务边界提交。

使用锁定 Codex 协议支持的 `optOutNotificationMethods` 关闭不必要的高频增量通知。方法名必须从锁定版本 Schema 验证。保留 turn/tool/token/rate-limit 和恢复所需通知。

先测 poll、callback、SQLite transaction、Outbox backlog、dispatch latency；只有实测证明单循环成为瓶颈才考虑拆分。

---

# 7. 最小 Team 与 Multi-Run

## 7.1 Team 仅有三种作用

1. Task 标签 / 归属；
2. 子预算；
3. 并发上限。

不建设 Team Leader Lease、成员 Presence、Team Message Bus、Agent Pool、Team Scheduler 或常驻 Lead Agent。

现有 Team 表属于更完整的旧 TeamManager 语义，本轮不直接启用整套模型。先评估最小的 Task `team_id` 归属和 Team 配置。

## 7.2 最小 Team 对象

概念字段：
`team_id, workspace_id, name, status, budget_ref, max_parallel_roots, config_json, created_at, updated_at`

Task assignment：
`task_id, team_id, assigned_at, assignment_source`

## 7.3 子预算

复用 `budgets.open_child`：
- Project/Run 父预算为上限；
- Team 子预算从父预算预留；
- 所有 Team 子预算之和不能超过父预算；
- Team 不得自行增加父预算；
- 调整由受信 Operator 或授权决策流程完成。

## 7.4 并发

准入同时检查：
- 全局 Root 上限；
- Run 上限；
- Team `max_parallel_roots`；
- Provider rate-limit capacity；
- Token budget exposure。

不新增 Team Scheduler。

## 7.5 Workspace 内多个 Run

一个 Workspace 可以创建多个 Run，每个 Run 固定 Workflow ID + Version，使用共享 Project Intent，但有独立 Run 状态和预算子额度。若当前 `run_id` 同时承担 Project 与 Workflow Execution 身份，先设计最小 additive `run_group` / Workspace-Run 关系，不建设 Project Registry。

---

# 8. Workflow 行为解耦

## 8.1 复用现有 WorkflowRegistry

当前 `orchestration/service.py` 已有 WorkflowRegistry / `default_registry()`。扩展现有 Registry，不创建第二套。

## 8.2 Workflow Package 行为挂点

Workflow Package 提供可选 Hook：

```python
class WorkflowHooks(Protocol):
    def validate_intent(self, context, intent_items): ...
    def build_worker_tools(self, context): ...
    def prepare_dispatch(self, context, task, attempt): ...
    def build_task_context(self, context, task, attempt): ...
    def reevaluate(self, context, changed_items): ...
    def replan(self, context, evidence, decisions): ...
    def prepare_job(self, context, proposal): ...
```

签名由实现 Agent 按真实调用点确定。没有实现的 Hook 必须使用明确默认行为或拒绝，不得静默跳过。

每个可信 Workflow Package 固定 ID/version/content digest，声明工具、Capability、资源和 Intent 验证规则，并继续受现有 Asset Governance 管理。

重点移除：
- `driver.py` 按具体 `workflow_id` 分支构建工具；
- `intent.py` 对具体方法名/Workflow 的直接 import；
- 核心中的具体 Workflow worker tool wiring；
- `reactor.drive_science` 对具体 Workflow 的硬编码；
- 静态 shipped workflow 清单造成的核心修改需求；
- CLI 对具体 Workflow 的分支。

第一版可使用安装时显式登记的可信包清单，不要求任意第三方动态加载。新增可信 Workflow 不应修改核心 Driver、Roots、Intent、Scheduler、Job Manager。

---

# 9. Agent Tool Gateway

## 9.1 复用已有工具

优先接入：
- `gx_job_submit`
- `gx_job_status`（按需查询，不用于模型持续轮询）
- `gx_job_cancel`
- `gx_capability_inspect`
- `gx_wait_job`
- 现有 Artifact 读取/提交工具

不得重复实现同名工具。`gx_wait_job` 是等待交接机制，不应让 Agent 循环轮询 Job。

## 9.2 一个通用 Artifact Analysis Job

增加一个受控通用能力，例如 `gx_artifact_analysis_submit`。

输入：
- Artifact refs；
- 分析脚本；
- 参数；
- 预期输出 Schema；
- 资源请求；
- 可选 Seed。

执行约束：
- 指定 Artifact 只读挂载；
- 无网络；
- Python 环境和依赖白名单；
- CPU、内存、磁盘、wall time、输出大小受限；
- 不暴露 GX DB、OAuth、API Key 或主机凭据；
- 通过现有 Job Admission/Budget；
- 输出经 Artifact Service 登记；
- 生成完整 Execution Manifest。

这不是开放 Shell，而是受控、可复现的分析 Job。必须做网络、路径穿越、沙箱逃逸和资源耗尽测试。

---

# 10. Codex 版本与 Provider Rate Limit

- 正常计量不读 rollout 文件。
- 固定当前已验证版本作为 baseline。
- 候选版本通过协议探针后扩大支持范围，不以硬编码版本字符串替代能力验证。
- 消费 Codex `account/rateLimits/updated` 等更新，记录 reset/可用状态并对新 Root/Turn 退避。
- 不自建 RPM/TPM 算法；未知 Provider 使用保守配置。

---

# 11. 配置简化

不引入四种复杂 Profile。保留四类简单配置：
1. Provider：Endpoint、Auth、Model Catalog、代理、Rate Limit；
2. Model：Model ID、Reasoning Effort、Context/Output 能力、价格元数据；
3. Role：角色到 Model、工具集和预算策略的映射；
4. Compute：CPU/GPU、显存、环境、容器、wall time。

代理可选，不固定 `127.0.0.1:7890/7891`。Reasoning Effort 按 Provider 能力校验，不写死特定档位。GPU 配置化，但保留 preflight、资源准入和硬上限。

---

# 12. P0 Service API 与 Runtime 精简

## 12.1 公共 Service API 提前

先移除 P0 对 v1 TeamService 私有方法的依赖。优先复用现有公共 Service；必要时提取：
- Task Submission
- Artifact Submission
- Lease
- Attempt Recovery
- Budget Admission

这些是接口边界，不要求机械创建五个新服务。

## 12.2 P0 Dependency Closure

分类：
- P0 必需；
- 兼容性；
- Optional；
- 历史遗留。

记录静态 import、动态 import、入口调用、DB 读写者、迁移/恢复和测试依赖。行数只是观测，不是删除目标。

验收：
- P0 不调用 TeamService 私有 API；
- 只依赖明确公共 Service Interface；
- v1 CLI 通过 compatibility adapter 隔离；
- 旧 DB 可恢复；
- 无调用方旧代码经证据确认后才退役。

---

# 13. 最小观测指标

只记录指导架构决策的指标：
- active Root / Turn / Native Child（可观测范围）；
- active Job；
- queue wait / dispatch latency；
- callback duration；
- SQLite transaction duration；
- lease renewal failure；
- usage reconciliation lag；
- estimated / confirmed / adjusted usage；
- provider rate-limit reset；
- budget maximum exposure。

暂不建设复杂优先级评分和大型监控平台。

---

# 14. 实施顺序

## Phase 1 — 解卡 + P0 Service API（最高优先）

1. `message --action` 直接走 GX Operator API，不转付费模型。
2. 增加预算耗尽后的本地 Operator Control Plane。
3. 改造 Thread 累计 Token 账本、估计结算和 adjustment。
4. 实时比较累计用量，硬预算触发在途 interrupt。
5. 调用现有 `jobs.leases.renew` 实现活动续租。
6. 通过协议关闭不必要增量通知。
7. 将回调内数值计算改为 Job。
8. 抽公共 P0 Service API，移除 TeamService 私有方法调用。
9. 移除 rollout 文件计量正常路径硬依赖。
10. 消费 Codex rate-limit 更新。

## Phase 2 — Workflow 行为挂点

复用 Registry，定义 Hook，迁移工具、Intent 验证、dispatch 准备、task context、reevaluate、replan、prepare_job。新 Workflow 不修改核心执行文件。

## Phase 3 — Agent 通用计算

接入已有 Job/Capability 工具，增加受控 Artifact Analysis Job，验证隔离、资源限制、Artifact 和 Manifest。

## Phase 4 — 最小 Team + Multi-Run

Task Team 标签、Team 子预算、Team 并发上限；一个 Workspace 内多个并行 Run。不得启用完整旧 TeamManager。

## Phase 5 — Runtime 精简与长程实测

建立 Dependency Closure，隔离 v1 compatibility adapter；执行 2h、8h、24h 分层实测和故障注入。根据实测决定是否需要更复杂并发设计。

---

# 15. 验收矩阵

## A. Billing / Budget
- A01 累计 Token 更新正确。
- A02 cached input 等类别按既定口径计入 GX Token Unit。
- A03 缺最终回执后自动估计结算。
- A04 后续累计值触发 adjustment。
- A05 ESTIMATED 与 PROVIDER_CONFIRMED 分开。
- A06 同一累计增量不重复结算。
- A07 多 Root 在途风险纳入硬预算。
- A08 达硬预算时请求 interrupt 并核对终态。
- A09 预算耗尽仍可本地 pause/cancel/budget_apply。
- A10 Turn 分摊未知不永久阻塞 Thread，总量账正确。

## B. Lease / Long-running
- B01 Root 活动超过默认 Lease 时长仍有效。
- B02 续租核对当前 Attempt/Lease/Fencing。
- B03 失效 Attempt 不能续租或提交。
- B04 Job Waiting 使用现有保护。
- B05 Provider Backoff 不消耗 Agent Failure Attempt。
- B06 GX 冷重启恢复 Root/Job/Lease。
- B07 用户暂停停止新增任务并安全 drain。

## C. Driver / Event
- C01 单一协调循环写入顺序正确。
- C02 不必要增量通知关闭。
- C03 必要 turn/tool/token/rate-limit 通知保留。
- C04 回调无长耗时科学计算。
- C05 回调无嵌套 App Server RPC。
- C06 Outbox 重复消费幂等。
- C07 callback、SQLite、dispatch 延迟有记录。

## D. Workflow Hooks
- D01 Intent Validator Hook。
- D02 Worker Tools Hook。
- D03 Prepare Dispatch Hook。
- D04 Task Context Hook。
- D05 Reevaluation/Replan/Prepare Job Hook。
- D06 缺失 Hook 有明确默认或拒绝。
- D07 新 Workflow 不修改核心执行文件。
- D08 旧 Workflow 精确版本可恢复。

## E. Agent Capability
- E01 Root 读取 Capability 描述。
- E02 Root 调用现有 Job Submit。
- E03 Root 调用现有 Capability Inspect。
- E04 Root 用 gx_wait_job 等待，不轮询。
- E05 Root 取消授权 Job。
- E06 分析脚本无网络。
- E07 只读指定 Artifact。
- E08 CPU/内存/时间/输出限制有效。
- E09 输出登记 Artifact + Manifest。
- E10 失效 Attempt 不能提交结果。

## F. Team / Multi-Run
- F01 Workspace 创建多个 Run。
- F02 每个 Run 固定 Workflow ID/Version。
- F03 Task 绑定 Team 标签。
- F04 Team 子预算复用 parent-child budget。
- F05 Team 并发上限生效。
- F06 Team 间隔离。
- F07 一个 Team pause 不影响其他 Team。
- F08 不创建 Team Agent Manager。

## G. Compatibility
- G01 旧 DB 冷恢复。
- G02 旧 CLI compatibility。
- G03 P0 不调用 TeamService 私有 API。
- G04 Capability Governance 无回归。
- G05 Codex App Server/Native Subagent 无回归。
- G06 不依赖 rollout 文件启动。
- G07 锁定版本协议探针通过。
- G08 Dependency Closure 有静态和动态证据。

---

# 16. 长程实测

- 2 小时：基本长程功能；
- 8 小时：多 Root 压力；
- 24 小时：进程故障注入和冷恢复；
- 更长期生产稳定性作为后续观察，不得把短测称为长期生产证明。

报告 Root/Turn/Team/Run/Job 数量、成功失败重试、Lease 续期、估计/确认/调整用量、最大预算暴露、Rate Limit、回调/SQLite 延迟和恢复时间。

---

# 17. Definition of Done

1. v2 无未解释回归。
2. P0 Service API 已抽取，不再调用 v1 私有 TeamService 方法。
3. 缺回执不会永久卡住，估计值不会伪装真实账单。
4. 硬预算能对在途工作执行有界中断。
5. 活动 Lease 可续期，失效 Lease 不可复活。
6. Driver 不被回调内计算阻塞。
7. Workflow 行为由可信 Workflow Package Hook 提供。
8. Agent 能自主提出受控分析 Job。
9. Team 只通过标签、子预算、并发上限实现。
10. 一个 Workspace 支持多个并行 Workflow Run。
11. Codex 原生 Agent Tree 完全由 Codex 管理。
12. 旧项目冷恢复、部署和回滚通过。
13. 所有通过项有源码、测试、命令、退出码和日志证据。

**最终定位：GX-H-v2.1 是以 GX 为控制面、官方 Codex 为 Agent Runtime、Job Runtime 为计算面的长程多任务 Harness。先把可靠执行和通用能力打牢，再进入 v3 Plugin Ecosystem 与 Research Intelligence。**
