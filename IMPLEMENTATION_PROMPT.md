# Coding Agent 总任务 Prompt：实现并验收 GX + Codex P0 Autonomous Science Harness

你是本项目的 Staff/Principal Engineer + Research Infrastructure Engineer。你的任务不是重新设计一个新框架，而是基于现有 GX-H-2 代码、指定版本的官方 Codex 源码和本包设计规格，实施一个精简、可运行、可故障恢复、可审计的 P0。

## 0. 最高优先级约束

1. 先阅读源码，后写代码。不得根据文档、文件名或架构图臆测实现。
2. Codex 使用官方上游 Runtime。默认不 Fork、不修改 Codex 核心，不重写 `run_turn`、`ThreadManager`、`AgentControl`、原生 Subagent、Session、Compaction。
3. 原生 Codex Subagent 继续用于短期协作，GX 不追踪它们的 Task 生命周期。
4. 需要跨会话、Job 等待、租约、预算、恢复或人类变更控制的长期任务，由 GX 创建独立 Codex Root Thread。
5. GX 是正式 Intent、Task/Attempt/Lease、Job、Resource Ledger、Execution Manifest、Evidence 的权威状态所有者。
6. 先提交 GX SQLite 事务，再调用 Codex 通知/投递 API。通知不是正确性基础。
7. 不新增 DSH/Cordis Runtime，不新增常驻 Director/Lead，不提前建设 20 个新模块。
8. 所有硬约束必须可机检才能做确定性拦截。自由文本约束只能通过任务书、审查和人工判断落实。
9. 不得把 schema、接口、mock 或静态代码阅读报告冒充为已实现功能。
10. 不得删除现有科学规则：决策者与 Evidence Reviewer 分离；Evidence 引用冻结 Evaluation；Measurement Failure 不形成反驳/支持证据。

## 1. 必须先完成的源码审计

### 1.1 GX
阅读完整当前仓库，特别检查：
- `persistence/records.py`
- `contracts/db_schema.sql`
- `contracts/states.json`
- `persistence/store.py`
- `orchestration/engine.py`
- `orchestration/reactor.py`
- `project_continuation.py`
- `jobs/runner.py`
- `jobs/executor.py`
- `jobs/service.py`
- `artifacts/service.py`
- `budgets/`
- `teams/briefs.py`
- `workflows/science_grid/nodes.py`
- `hostobs/` 现有 Codex 接入
- 所有已有 tests

必须找出每个既有对象的真实写入方、调用方、状态转换和测试。特别核对哪些表只有 schema 没有写入语句。不要重复建设已有能力。

### 1.2 Codex
执行 `scripts/fetch_codex_source.sh`，核对：
- tag `rust-v0.169.0`
- 预期 commit prefix `d32063f9`
- App Server protocol schema
- `thread/start`, `thread/resume`
- `turn/start`, `turn/steer`, `turn/interrupt`
- `thread/goal/set/get/clear`
- Goal 状态、token budget、Goal 更新注入、子 Agent budget 语义
- main thread 禁用 Goal 的配置路径
- `client_user_message_id` 与真实 human input 事件来源
- Thread resume、App Server restart、compaction、dynamic tools 恢复
- 原生 Subagent 在父 Thread 中的权限/工具继承

按锁定 Codex checkout 生成 TypeScript 和 JSON Schema：
```bash
codex app-server generate-ts --out ./generated/codex-schema
codex app-server generate-json-schema --out ./generated/codex-schema
```
如果该版本命令/输出不同，以源码为准并记录。

若 tag/commit 不匹配，停止所有依赖版本的实现，报告证据和建议，不要静默换版本。

## 2. 先交审计结果，不要立即大改

输出：
1. `AUDIT_REPORT.md`：GX 与 Codex 源码事实、路径、行号、调用方、未核实事项。
2. `REUSE_DELETE_REWRITE.md`：直接复用、改造、删除、全新实现清单。
3. `P0_PLAN.md`：按 PR 拆分的实施顺序、风险、回滚。
4. 运行当前测试并保存基线报告。
5. 标记所有静态推断与实测事实。

审计后继续实施，不需要因普通实现细节等待用户批准；但任何扩大架构范围、修改 Codex 核心、改变授权规则、放宽硬约束的动作必须停下并提出 ADR。

## 3. P0 目标范围

按 `DESIGN_SPEC.md` 实施：
- Versioned Intent Item
- Human Input Authorization
- Supplement / Explicit Change / Ambiguous Proposal
- Item-level Change Impact Analysis
- SQLite transaction + Outbox
- Native Codex App Server Adapter
- Codex Root Thread for Durable Task
- 原生 Subagent 保持原样
- Task / Attempt / Lease / Fencing
- GX Event Loop
- Job async submit / observe / reconcile
- Execution Manifest
- Four-way settlement
- 最小 Hypothesis / Evidence / Evaluation / Decision 写入路径
- Continue / Prune / Pivot / Stop
- 多轮消息投递、对账、恢复

不在 P0 范围：
- DSH / Cordis
- 新的 Agent Loop
- 新的 Agent Pool
- 多 Runtime
- 通用 Workflow Search
- Multi-Model Council 独立框架
- Agent Quality Ranking
- Research Portfolio
- 自动 Skill / Plugin 安装
- 完整知识图谱
- Codex Rust 扩展或 Fork（除非独立 ADR 证明必要）

## 4. 多轮消息机制：必须按以下规则实现

### 4.1 三种输入处理结果
- Supplement：新增来源/数据/结果，不修改 Intent 版本。可能触发 Evidence/Decision。
- Explicit Change：明确修改某些 Intent Item，进入 Impact Analysis。
- Ambiguous Proposal：含糊表达只形成提案，必须在主对话向用户澄清，不自动提交。

这三种只是展示标签。最终影响处置必须由具体条目差异、依赖关系和任务影响计算决定。

### 4.2 授权
- GX 在真实人类输入到达时建立授权记录。
- 优先使用 `client_user_message_id` 作唯一来源键，但必须先核验锁定版本协议。
- 提交时从受信任协议信封的 `thread_id` / `turn_id` 查授权。
- 模型不得传入授权 ID、来源 thread ID、turn ID 作为可信参数。
- 网页、论文、MCP、Shell、工具输出不构成人类授权。
- 只有主对话 Thread 的真实人类输入 Turn 有提交资格。
- 放宽/移除硬约束、替换 Objective、提高资源上限：无论是否有在途影响，都必须后续明确人类 Turn 二次确认。
- 收紧硬约束或降低资源上限：只有意图明确、条目结构化、影响分析完成时才可提交。
- 讨论、猜测、评价不等于正式修改。

### 4.3 条目级版本和影响
- 每个 Intent Item 独立 version。
- Task/Workflow/Job 保存自己实际依赖的 item_id + version/hash。
- 修改一个条目，只重算依赖它的对象。
- 评估标准改变时，尽量复用生成/结构产物，重新评估即可。
- 旧产物不删除；可能标记 historical / not-valid-for-current-intent。
- 事务内重新查询受影响 Task/Attempt/Job，避免并发竞态。
- 不能按输入分类标签直接取消整条 Path 或重建全部 Thread。

### 4.4 先提交，再通知
SQLite 一个事务内完成：
- 授权检查
- 条目版本 CAS
- Intent Item 新版本
- 影响对象计算/记录
- 相关旧 Attempt Lease/Fencing 失效
- Change Record
- Outbox Events

事务提交后才调用 Codex：
- 活跃 turn：`turn/steer`，传真实 `expectedTurnId`
- 空闲 Thread：`turn/start`
- 任务彻底作废：先 GX fencing，再 `turn/interrupt`
- 新任务书：新 Attempt + 新 Root Thread

投递失败不回滚 Intent；通过 Outbox retry/reconcile 恢复。

Change Record 是 GX 权威记录；Change Notification 是给 Thread 的简短说明。不要混用“变更卡”一词。

## 5. Codex 原生机制

- 通过官方 App Server JSON-RPC。
- 初始化握手必须正确。
- 保持双向读取响应和 notifications。
- 不能只实现 request/response 而丢弃 notifications。
- 正确处理请求超时、进程退出、未知结果、断线重连。
- `turn/steer` 只对活动 turn 使用，核对 expectedTurnId；否则 `turn/start`。
- Native Goal 只作为单 Thread 目标/token budget 投影。
- GX 负责项目级 GPU/CPU/API/token 预算。
- `thread/goal/set` 可能替换 objective 并重置 token usage；不得每次小变更都重设。
- 主对话 Thread 关闭模型自建 Goal/自动续跑，具体机制以 pinned source 验证。
- Job 等待期间 Thread 不应持续占用模型。Native Goal paused/active 与外部 Job 的交互必须原型验证。
- 不修改 Codex 原生 Subagent。短期协作由 Codex 自己管理。
- 不通过扫描 rollout 推断 Subagent 与 GX Task 的关系。

## 6. 硬约束与执行安全

- GX Job/Tool/Capability 准入入口检查结构化硬约束。
- GX 外的 Codex Shell 原生执行不受 GX 自动拦截。
- 如必须执行前拦截，研究 Codex PreToolUse Hook 或受控执行环境，量测成本后提出最小方案。
- 所有最终 Evidence 必须引用带 Execution Manifest 的 Job/Artifact。
- Manifest 对照当前有效硬约束做验收。
- 不合规结果可以保留，但不能成为当前需求下的有效 Evidence。
- 模型不能修改授权、预算、硬约束或提交自己的结果为已验收状态。

## 7. P0 验收测试

至少包括：
1. Codex App Server initialize/start/resume/turn/notifications。
2. 原生 Subagent 正常工作，GX 不接管。
3. Durable Root Thread 创建、恢复和重建。
4. Goal set/get/clear、token budget 与 Job wait 配合。
5. 主对话 Thread Goal 禁用配置。
6. steer 对长命令的真实延迟；无活动 turn 时 fallback。
7. 非 OpenAI Provider 下 compaction 与任务书恢复（若该 provider 在 pinned version 支持）。
8. Human Turn 授权；Agent/网页/工具输出伪造授权必须失败。
9. 模糊输入只产生 Proposal。
10. Supplement 不改变 Intent version。
11. 条目级版本变化只影响依赖对象。
12. 放宽限制/替换 Objective/提高资源上限需要二次人类确认。
13. 评估标准变化复用上游产物。
14. 事务提交后通知失败，Outbox 重放成功。
15. 重复 Outbox 不重复产生有效动作。
16. Intent commit 与 Job completion 并发，两种事务顺序均正确。
17. 旧 Attempt fencing 后无法提交。
18. Job 提交成功但回执丢失时先对账。
19. 硬约束在 GX Job 准入被拒绝。
20. 不受 GX 控制的 Shell 路径违规能在验收中被发现；若承诺预防，必须启用 Hook/受控环境。
21. Measurement Failure 不产生 Evidence。
22. 决策者与 Reviewer 不同。
23. Evidence 引用冻结 Evaluation。
24. 每个 Job 有完整 Execution Manifest。
25. Continue / Prune / Pivot / Stop 正确推进。
26. 至少连续三轮端到端合成科研闭环。

## 8. 故障注入

必须实际杀进程或模拟真实进程故障：
- Kill GX during transaction.
- Kill Codex App Server during active turn.
- Kill App Server after thread/start but before GX saves response.
- Drop notification after successful Codex request.
- Job backend accepted submission but response lost.
- Job completes concurrently with Intent change.
- Old Thread resumes after Lease invalidated.
- duplicate Outbox delivery.
- long-running shell command while steer is sent.
- compaction then resume.
- non-OpenAI provider compaction where supported.

对每个故障记录：预期状态、实际状态、是否人工介入、是否重复副作用、恢复耗时。

## 9. 交付物

必须输出：
- `AUDIT_REPORT.md`
- `ARCHITECTURE_DECISIONS.md`
- `P0_IMPLEMENTATION_PLAN.md`
- 实际代码与迁移
- Codex App Server Adapter
- Intent/Authorization/Change/Outbox
- Event Loop
- Task/Attempt/Lease/Fencing
- Job/Manifest/Validation/Evidence
- 自动化测试
- `ACCEPTANCE_REPORT.md`
- `FAILURE_INJECTION_REPORT.md`
- `REUSE_DELETE_REWRITE.md`
- `KNOWN_LIMITATIONS.md`
- 运行命令与部署说明

每个已完成项必须链接到代码、测试和运行证据。未实现、mock-only、静态阅读、无法验证的项目必须明确标注。

## 10. 工作方式

- 每个阶段小 PR，先测试后合并。
- 不做大规模无关重构。
- 不删现有 GX 功能，除非有调用方审计、替代方案和回滚。
- 不因“架构更优雅”而增加新服务或新层。
- 不允许跳过基线测试。
- 发现与规格冲突时，先写 ADR，不要自行扩大范围。
- 每个阶段结束提交简短报告：改了什么、复用什么、删除什么、测试结果、风险、下一阶段。
- 先做 P0，不要提前实现 P1/P2/P3。

开始：先读取 `DESIGN_SPEC.md`、`CODEX_SOURCE_AND_API.md`，再检查当前 GX 仓库和 Codex 源码。先完成源码审计与基线报告，再开始实施。
