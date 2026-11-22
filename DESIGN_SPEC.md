# GX + Codex Science Harness — P0 完整设计规格

版本：P0 Design Baseline  
目标：在不修改 Codex 核心、不重写原生 Multi-Agent 的前提下，建立可靠的长程科研执行闭环与多轮人机需求变更机制。

---

## 0. 架构决策摘要

### 0.1 两个核心系统

**Codex Runtime（尽量使用上游原生实现）**
- Agent Loop / Turn 执行
- 原生 `spawn_agent` / Subagent
- Thread / Session / Rollout
- Tool Orchestrator
- Context / Compaction
- Sandbox / Permission
- 模型执行与原生工具

**GX Scientific Execution Kernel**
- Versioned Intent：正式需求、硬约束、软偏好、评估规格、资源条目
- Durable Task：Task / Attempt / Lease / Thread binding
- Job：异步计算、排队、取消、结果未知对账
- Event Loop：常驻消费 Outbox / Reaction 并推进状态
- Resource Control：模型 token 之外的 GPU、CPU、外部 API、项目总预算
- Artifact / Evidence / Execution Manifest：来源、完整性、验收
- Codex App Server Adapter：创建/恢复 Thread、启动/Steer/Interrupt Turn、Goal 投影、事件消费

### 0.2 两种 Agent 生命周期，不可混淆

**Ephemeral Native Subagent**
- 在单个 Codex Thread/Turn 的局部协作中由 Codex 原生派生。
- 用于短期探索、论文查证、并行小任务、代码辅助、反例寻找。
- GX 不为它创建 Durable Task，不做事后 spawn 反推，不维护其租约。
- 子 Agent 继承的 GX 工具必须按来源 Thread/Turn 做权限判断；原生 Subagent 的 GX 写工具调用默认只读/拒绝。

**GX Durable Task + Codex Root Thread**
- 仅当任务需要跨会话/重启、等待外部 Job、受人类变更影响、需要预算/租约/验收/恢复时使用。
- GX 创建逻辑 Task/Attempt，并通过 Codex App Server 创建独立 Root Thread。
- Root Thread 内仍然允许使用 Codex 原生 Subagent。
- 任务不是永久绑定 Thread：一个 Task 可有多个 Attempt，一个 Attempt 可绑定一个执行 Thread；Thread 可以重建或交接。
- GX 不接管 Codex 内部 Agent Tree。

### 0.3 不建设的东西

- 不 Fork Codex 核心；不改 `run_turn`、`ThreadManager`、`AgentControl`。
- 不重写原生 Subagent、Inbox、Session、Compaction。
- 不接入 DSH Runtime / Cordis。
- 不新增 Director/Lead 常驻身份；研究决策是一种 Task 类型。
- 不把所有短期 Agent 都变成 GX Durable Task。
- 不为了概念完整增加 Goal/Mission 层级；沿用 Objective / Path / Workflow / Task / Attempt / Job。
- 不提前建设 Agent Pool、Multi-Model Council、通用 Workflow Search、Research Portfolio、Agent Quality 排名、自动 Skill/Plugin 安装。
- 不用自由文本直接强制执行硬约束；硬约束必须可机检，否则作为任务书约束和审查要求。

---

## 1. 状态所有权

| 对象 | 唯一权威所有者 | 说明 |
|---|---|---|
| 主对话 | Codex 主对话 Thread | 保存自然语言对话与 turn 历史 |
| 正式需求 Intent | GX SQLite | 条目级 ID、版本、类型、结构化值、来源 |
| Objective / Path | GX SQLite | 沿用 GX 现有术语，不新增 Mission |
| Task / Attempt / Lease | GX SQLite | 正式任务状态与尝试 |
| Codex Thread ID | Codex 生成，GX 保存映射 | Thread 是执行上下文，不是科研状态 |
| Codex 原生 Subagent | Codex | GX 不追踪其临时生命周期 |
| Job | GX SQLite + 后端真实状态 | Job 脱离 Thread 生命周期 |
| Artifact | GX Artifact Store / 文件系统 | 内容 Hash、不可变发布、来源 |
| Evidence / Evaluation / Decision | GX SQLite | 仅写入闭环实际需要的最小字段 |
| Token 用量 | Codex 提供观测，GX 汇总账本 | Codex Goal 可约束单 Thread token |
| GPU/CPU/API/项目预算 | GX | 跨 Thread、跨 Job 的总额和准入 |
| Skills | 文件/仓库版本控制 | 方法说明，不是权威执行状态 |

规则：每类状态只有一个权威写入者。Codex 事件可投影进 GX，但不允许 GX 与 Codex 同时成为同一状态的双写者。

---

## 2. 核心科研闭环（P0）

```text
Human Objective / Requirements
          |
          v
Two or more competing Hypotheses
          |
          v
Research Paths
          |
          v
Frozen Evaluation / Experiment Specification
          |
          v
Workflow Plan (small controlled variants)
          |
          v
GX Task Dispatch -> Codex Root Thread
          |               |
          |               +--> Native Codex Subagents for ephemeral collaboration
          |
          +--> GX Job submission for long compute
          |
          v
Validation (deterministic + independent review)
          |
          v
Four-way settlement
          |
          +--> Infrastructure failure
          +--> Agent execution failure
          +--> Measurement failure
          +--> Valid scientific outcome (supports/refutes)
          |
          v
Evidence update (only valid measurements)
          |
          v
Decision Task: continue / prune / pivot / stop
          |
          v
Next research round or close
```

P0 验收用例固定使用两个竞争假设和两条配对路线，但引擎不能硬编码只能有两个；数量应由配置决定。

### 2.1 保留的科研规则（不得回退）

1. 决策者与证据审查者必须分离。
2. Evidence 必须引用冻结版本的 Evaluation 输出。
3. 测量失败不构成支持或反驳证据。
4. 只有测量有效且结果与预测一致/相反，才形成支持/反驳证据。
5. 有效负面结果是科研知识，不是系统执行失败。
6. 旧路线/旧目标产生的结果保留历史，不因新需求而删除。

### 2.2 四类结算

| 类别 | 含义 | 是否消耗 Agent Attempt | 是否形成 Evidence |
|---|---|---:|---|
| Infrastructure Failure | API、网络、Runtime、节点故障、Job 后端故障 | 默认不消耗科学执行 Attempt；有独立恢复计数 | 否 |
| Agent Execution Failure | 输出不合 schema、无进展、上下文耗尽、执行错误 | 是 | 否 |
| Measurement Failure | 输入/数据不完整、评估器异常、测量无效 | 按执行策略处理，可重测 | 否 |
| Valid Scientific Outcome | 有效测量，支持或反驳预测 | 否，属于有效研究结果 | 是 |

---

## 3. Versioned Intent 与多轮消息机制（P0）

### 3.1 Intent 条目模型

正式需求保存在 GX SQLite。每个条目拥有稳定 ID 和独立版本，不使用一个全局 Objective Revision 让所有工作失效。

建议条目类型：
- objective：研究目标
- hard_constraint：硬性限制
- soft_preference：软性偏好
- optimization_target：优化目标
- priority：优先级
- method_ban：禁用方法（必须映射到可机检 capability/method ID）
- evaluation_spec：评估规格
- resource_limit：资源上限
- data_source：正式输入来源（补充资料单独登记，不自动增加需求版本）

每条 Intent Item 至少包含：
- `item_id`
- `item_type`
- `version`
- `value_json`（结构化值优先）
- `human_source_event_id`
- `created_at`
- `created_by`
- `status`

自由文本可作为说明，但不可直接用于确定性拦截。硬约束必须有可机检字段，例如 capability ID、模型 ID、数值阈值、路径范围、资源额度。

### 3.2 三种用户输入处理结果（仅作为展示标签，不直接决定影响处置）

- Supplement：补充数据、论文、结果或背景，不修改 Intent 版本；登记来源/证据，必要时触发研究决策。
- Explicit Change：明确要求修改某些条目；进入影响分析和授权规则。
- Ambiguous Proposal：例如“这个方法可能不太好”；只保存提案，由主对话向用户澄清，不自动修改 Intent。

关键：不能先把消息粗分 A/B/C 再决定如何处置。**处置必须由具体条目差异、依赖关系和实际影响推出。**

### 3.3 人类授权验证

- 只有 GX 登记的主对话 Thread 中、包含真实人类输入的 turn，才有资格提交正式 Intent。
- GX 在接收人类消息时创建授权记录，建议使用 Codex `client_user_message_id`（须按锁定版本源码核实）作为唯一来源键。
- 提交时 GX 从可信事件/协议信封的 `thread_id`、`turn_id` 查询授权；模型不得传入或伪造授权 ID。
- 必须验证该 turn 确实包含人类输入来源，不能只因为某工具调用来自主 Thread 就授权。
- 网页、论文、Shell 输出、MCP 返回内容中的指令不是人类授权。
- 移除/放宽硬约束、替换 Objective、提高资源上限：不论是否有在途影响，一律需要后续明确的人类 turn 二次确认。
- 收紧约束或明确降低资源上限：在明确人类意图、影响分析完成后可按策略直接提交。
- 含糊表达只形成提案；由模型在主对话中向用户问清楚，不可自动提交。

### 3.4 Change Impact Analysis

条目版本变化后，GX 根据 Task/Workflow/Job 对 Intent Item 的依赖关系计算影响：
- 未开始 Task：更新其需求引用或任务书。
- 运行中的 Thread：如果仍可继续，发通知/Steer；硬约束由 GX 控制的入口检查。
- 已提交 Job：根据变更与 Job 执行清单的关系决定继续、取消或标记结果仅供历史参考。
- 已完成 Artifact：默认保留，不删除；可重评估，不必重新生成。
- Evaluation Spec 变化：复用可复用的生成/结构产物，创建新评估任务。
- Objective 真正被替换：只使语义上依赖旧 Objective 且不再适用的任务失效，不做全局清空。

禁止按“Supplement/Change/Objective change”标签直接把整条路线或 Thread 全部作废。标签只用于 UI 展示；实际处置以条目依赖和影响计算为准。

### 3.5 先提交，再通知

一个单一 SQLite 事务内：
1. 验证人类授权。
2. 验证条目当前版本（乐观并发）。
3. 写入 Intent Item 新版本。
4. 计算并持久化受影响对象（必要时在事务内重新查询受影响任务集合）。
5. 使相关旧 Attempt 的 Lease/Fencing Token 失效。
6. 写入 Change Record。
7. 写入 Outbox Delivery Events。

事务提交后再通过 Codex 原生 API 投递。投递失败不回滚已提交 Intent；Outbox 重试和对账。

### 3.6 Change Record 与 Change Notification

二者必须区分：
- Change Record：GX SQLite 中的权威变更记录，包含修改条目、旧/新版本、来源 turn、影响分析、审批状态、事务时间。
- Change Notification：发送给 Codex Thread 的简短消息，只通知变化和下一步，不是权威状态。

---

## 4. Codex 原生机制与接入规则

### 4.1 原则

- 不修改 Codex Agent Loop、ThreadManager、AgentControl、原生 Subagent。
- GX 通过 Codex App Server JSON-RPC 调用官方协议。
- Codex 原生 `spawn_agent` 继续处理临时协作。
- GX Durable Task 使用独立 Root Thread。
- Root Thread 内部仍可派生原生 Subagent。
- GX 不通过扫描 Codex rollout 反推子 Agent 与 Task 的关系。
- 不把 DSH/Cordis 引入主架构。

### 4.2 主要 API（必须按锁定源码版本生成 schema 并核验）

- `initialize` + `initialized`
- `thread/start`
- `thread/resume`
- `turn/start`
- `turn/steer`
- `turn/interrupt`
- `thread/goal/set`
- `thread/goal/get`
- `thread/goal/clear`
- 必要的 Thread/Turn/Item 事件通知

注意：
- `turn/steer` 只适用于正在运行的指定 turn，必须传正确的 `expectedTurnId`；没有活动 turn 时应使用 `turn/start`。
- `thread/goal/set` 新 objective 可能替换原 Goal 并重置其 token usage accounting；不得每次小变更都无脑重设 Goal。必须通过锁定版本源码/测试验证。
- 主对话 Thread 必须关闭原生 Goal 自动续跑/模型自建 Goal 的相关能力（具体配置以源码核验）。
- Native Goal 只作为单 Thread 的目标/token budget 投影；GX 是跨 Thread、GPU、CPU、API、项目预算的唯一账本。
- GX 从项目总额为每个 Attempt 分配预算；创建 Thread 时投影 token budget；结束时读取实际用量并结算。
- GPU/CPU/外部 API/项目总预算由 GX 强制准入和记账。

### 4.3 线程任务书

GX 创建 Durable Task Root Thread 时，使用 Codex 原生 thread/start 配置和 `developer_instructions`（字段/语义须按锁定版本核实）注入任务书。任务书包含：
- Objective 与相关 Intent Item 的 ID/version/hash
- Task 描述和验收条件
- 允许的能力与硬约束
- 资源预算与执行 Profile
- 已有结果、Artifact 引用、前次 Attempt 摘要
- 需要继续的事项
- GX 工具调用边界

任务书是初始上下文，不是权威状态。Codex Compaction 后是否重新插入、非 OpenAI Provider 是否一致，必须原型验证。

### 4.4 Goal 与 Job 等待

目标方案：Thread 执行完当前 turn 后进入等待，不占模型。Job 完成后由 GX 唤醒。
- 研究 Thread 的 Native Goal 用于单 Thread 目标和 token budget。
- Job 等待时是否将 Native Goal 状态置为 paused、完成后设 active，必须验证锁定版本状态枚举和续跑语义。
- GX Task/Attempt 状态才是权威等待状态。
- Job 完成事件由 GX Event Loop 消费，完成对账后调用 `turn/start` 续做。
- 不能假设 Native Goal 本身会可靠等待外部 Job 或自动唤醒。

### 4.5 多轮消息机制

主对话：
1. Codex 主对话 Thread 接收人类消息。
2. GX 通过受信任的 App Server 事件/客户端输入路径识别人类输入。
3. GX 建立 `HumanInputAuthorization`，来源键优先核验 `client_user_message_id`。
4. 主对话 Agent 可以调用 GX 提案/提交工具，但授权由 GX 根据调用信封的真实 thread_id/turn_id 查询，不能由模型参数传入。

任务 Thread：
- Supplement：GX 登记来源；可向相关 Thread 发通知，但不修改 Intent 版本。
- Explicit Change：先完成影响分析和授权规则，再提交事务，后投递通知。
- Ambiguous Proposal：只保存提案；主对话澄清。
- Change Notification 根据线程状态选择：
  - 活跃 turn：`turn/steer`（带 expectedTurnId），只作通知。
  - 无活动 turn：`turn/start` 新 turn。
  - 需要废止当前尝试：先使 GX Lease/Fencing Token 失效，再 `turn/interrupt`；旧 Thread 的后续提交必须被拒绝。
  - 需要全新任务书：创建新 Attempt 和 Root Thread，旧 Thread/产物留作历史。

### 4.6 需求通知不承担正确性

- 软偏好可以靠通知更新。
- 硬约束必须在 GX 控制的 Job/Tool/Capability 准入处检查。
- Codex 原生 Shell 等不经 GX 的执行路径，GX 无法声称已在执行前拦截。
- 如果硬约束必须预防而非事后发现，应使用 Codex PreToolUse Hook 或受控执行环境；前者可能每次工具调用增加进程启动成本，需量测。
- 最终验收时，Evidence 必须引用带 Execution Manifest 的 Job/Artifact；对照当前有效硬约束检查。
- 不合规结果可以保留为历史 Artifact，但不能被接受为当前需求下的有效 Evidence。

---

## 5. Execution Manifest 与可复现性（P0）

从第一个 Job 起记录，不可事后补造。

至少包含：
- Job ID、Attempt ID、Task ID
- 输入 Artifact IDs + content hashes
- Dataset ID/version/hash
- Git commit / source tree digest
- Tool/Capability ID、版本、内容摘要
- Model/provider/weight version（可获得时）
- 完整参数、随机种子
- Container image digest / dependency lock
- GPU 型号、驱动、CUDA/runtime
- 后端类型与后端 Job ID
- 开始/结束时间、退出码、日志引用
- 输出 Artifact IDs + hashes
- Evaluator ID/version/spec hash
- 运行状态、复现等级、已知非确定性因素

区分：
- Provenance：来源可追溯。
- Reproducibility：能否重新得到相同/近似结果。
- Replayability：能否重放当时执行过程。

不承诺所有远端 API 或 GPU 浮点计算 bitwise reproducible。

---

## 6. Event Loop 与 Outbox（P0）

GX 使用一个常驻事件循环，不额外拆成多个调度微服务。

循环职责：
1. 拉取未处理 Outbox/Reaction。
2. 按事件类型调用确定性 handler。
3. 重新读取当前对象状态和版本。
4. 执行 Workflow 的可运行确定性节点。
5. 为 Durable Task 创建/恢复 Codex Root Thread。
6. 提交/观察/对账 Job。
7. 投递 Codex Native API 请求。
8. 保存投递回执与消费游标。
9. 失败退避重试。

正确性要求：
- 至少一次投递 + 幂等消费。
- Event ID 唯一。
- 外部副作用有 idempotency key。
- 先写操作意图，再执行外部操作。
- 结果未知时对账，不盲目重投。
- 需求变更与 Job 完成并发时，以 SQLite 事务顺序为准；事务内重新查询受影响任务集合。
- 重复事件不得重复产生有效科研结果。

---

## 7. 四类结算与 Evidence 约束

每个 Task Attempt 结果经过：
1. Submission：Agent 提交结果，不代表成功。
2. Deterministic Validation：schema、文件、hash、manifest、硬约束。
3. Independent Review：不同 Agent/模型；只给主张、评估输出、产物和必要原始输入，不给作者推理过程。
4. Settlement：由 GX 根据验证与审查结果确定状态。

规则：
- 决策 Agent 与 Evidence Reviewer 不同。
- Evidence 必须引用冻结版本的 Evaluation 输出。
- Measurement Failure 不得生成支持/反驳 Evidence。
- 只有 Valid Scientific Outcome 才能更新假设证据关系。
- Red Team 是一种低成本独立审查 Task，不是常驻 Agent。
- 模型自报置信度仅作注释，不作为控制信号。

---

## 8. Workflow 变体比较（P1，受限版）

- 仅在研究闭环跑通后实现。
- 同一份冻结实验/评估规格下，比较少量（初期 2–3 条）路线变体。
- 配对比较，尽量控制数据、预算、评估器、模型版本等混杂因素。
- 不做通用无限 Workflow Search。
- 不因为单次小样本差异就宣称统计显著。
- 记录成本、成功率、质量指标和不确定性。

---

## 9. P0 实施顺序

### Phase 0 — Audit & Baseline
- 阅读当前 GX 全仓库。
- 阅读指定 Codex 源码版本。
- 核验现有实现、schema、调用方、测试。
- 运行现有测试并建立基线。
- 不改行为。

### Phase 1 — Codex App Server Prototype
- 官方 App Server 客户端连接、initialize。
- thread/start、turn/start、事件流。
- thread/resume。
- thread/goal/set/get/clear。
- turn/steer、turn/interrupt。
- Goal 暂停/恢复与长 Job 等待原型。
- 主对话关闭 Goal 配置原型。
- 不改 Codex 源码。

### Phase 2 — Intent + Authorization + Change
- Intent Item 级版本化。
- HumanInputAuthorization。
- Supplement / Explicit Change / Ambiguous Proposal。
- Change Record、Impact Analysis、二次确认规则。
- SQLite 单事务 + Outbox。
- Change Notification 投递和对账。

### Phase 3 — Durable Task + Root Thread
- Task/Attempt/Lease/Fencing。
- GX 创建 Root Thread。
- 原生 Subagent 保持 Codex 管理。
- Wake Packet 由 GX 编译。
- 任务完成、失败、重建、交接。
- 不扫描 rollout 反推原生子 Agent 任务关系。

### Phase 4 — Job + Manifest + Resource
- Job 异步提交、观察、取消、对账。
- Execution Manifest。
- GPU/CPU/API/Token 项目预算。
- 结果未知与幂等恢复。

### Phase 5 — Validation + Evidence + Decision
- 四类结算。
- 独立审查。
- 冻结 Evaluation 引用。
- 最小 Hypothesis/Evidence 关系。
- Continue / Prune / Pivot / Stop 决策任务。
- 下一轮自动推进。

### Phase 6 — P0 End-to-End Acceptance
- 连续三轮合成科研闭环。
- 故障注入。
- 成本与恢复数据。
- 输出证据包。

P1/P2/P3 不得在 P0 未通过时提前实现。

---

## 10. Codex 源码使用规则

- Codex 基线暂定 `rust-v0.169.0`，预期 commit 前缀 `d32063f9`（来自本项目蓝图静态审计，必须重新核验）。
- 使用上游 Codex App Server。
- 不复制或维护 Codex 内部 Agent Loop。
- 不改 Codex `AgentControl` / `ThreadManager` / `run_turn`。
- 如果发现 App Server 缺少必要接口，先记录阻塞和最小原型；只有实测证明外部接入无法满足，才提出最小扩展 ADR。
- 所有协议字段必须以该版本 `codex app-server generate-ts` / `generate-json-schema` 输出和源码为准。
- 任何扩展 API 都是内部接口，默认不采用。

---

## 11. P0 验收门槛

必须通过：
- 两个竞争假设、两条配对路线，完成至少三轮。
- 不需要 Root 手工推进 Workflow。
- Job 等待期没有无意义模型轮询。
- App Server 重启后能恢复 GX Durable Task。
- Job 回执丢失不造成重复执行。
- 需求条目变更只影响实际依赖对象。
- 评估标准改变时能复用上游产物并重新评估。
- 模糊表达不提交需求。
- Agent/网页/工具输出不能伪造人类授权。
- 放宽硬约束、替换 Objective、提高资源上限必须二次人类确认。
- 失效 Attempt 不能提交有效结果。
- 违反可机检硬约束的 Job 在 GX 控制的准入入口被拒绝。
- 非 GX 执行路径的限制至少在最终验收中被发现；若要求执行前阻止，必须启用受控执行路径/Hook。
- Measurement Failure 不形成科学 Evidence。
- 决策者与 Evidence Reviewer 不同。
- 每个 Job 都有 Execution Manifest。
- Continue / Prune / Pivot / Stop 能正确结束或推进研究。
- 所有测试、故障注入和关键指标有可审计报告。

禁止以“接口已定义”“schema 已存在”“mock 测试通过”作为功能完成证明。
