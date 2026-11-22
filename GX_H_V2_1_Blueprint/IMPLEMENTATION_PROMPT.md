# Coding Agent Prompt — GX-H-v2.1 Architecture Correction & Implementation

你是 GX-H-v2.1 的 Principal Engineer、Runtime Architect、Database/Concurrency Engineer、SRE 和 Acceptance Owner。

基于 `https://github.com/AshHuHK/gx-h-v2` 最新代码，自主完成源码审计、架构细化、实施、部署和验收。目标是将 v2 增量演进为可靠的 Multi-Run、Multi-Team、Multi-Root、Long-Running Harness，不重写 Codex，也不建设 v3 Plugin Host。

## 0. 最新源码审计

当前审查基线 HEAD：`2314eed72822387e03378e8e676ad5fa9228a8d0`。实施前必须读取最新 HEAD；如有变化，先比较并保留新提交。

亲自运行基线测试和关键复现。用户提供的是静态审查，不可直接当成运行事实。

重点检查：
- `p0/driver.py`
- `p0/roots.py`
- `p0/root_delivery.py`
- `p0/waits.py`
- `p0/profiles.py`
- `p0/intent.py`
- `jobs/tools.py`
- `jobs/leases.py`
- `capabilities/tools.py`
- `capabilities/registry.py`
- `orchestration/service.py`
- `orchestration/engine.py`
- `budgets/`
- `contracts/db_schema.sql`
- `teams/`
- `workflows/`
- 全部 tests / migrations / acceptance。

先输出：
- `V21_AUDIT_REPORT.md`
- `V21_REUSE_DELETE_REWRITE.md`
- `V21_ARCHITECTURE_DECISIONS.md`
- `V21_IMPLEMENTATION_PLAN.md`
- `V21_ACCEPTANCE_PLAN.md`

必须给出真实调用方、状态 Owner、事务边界、复现结果和风险。

## 1. 绝对约束

- 不修改/Fork Codex，不重写 Agent Loop、Native Subagents、Thread、Session。
- 不引入 DSH/Cordis、Temporal、LangGraph、Prefect。
- 不建设 Plugin Host。
- 不建设 Team Agent Manager、Team Scheduler、常驻 Lead Agent。
- 不建设 Project Registry；一个状态目录仍是一个 Project Workspace。
- 不重复实现已有 Job、Capability Registry、Budget、Outbox、Lease。
- 不开放任意 Shell、网络或宿主机文件访问。
- 不把未知用量当 0 或伪装成真实账单。
- 不让 Agent 自行修改正式 Intent、预算或 Evidence。
- 不大规模搬目录或删除历史代码。
- 保持旧 DB、CLI、Codex Root、Job 和冷恢复兼容。

## 2. Phase 1 — Unblock + P0 Service API（最高优先）

实施：
1. `message --action` 结构化 Operator Action 直接走 GX，不转付费模型。
2. 预算耗尽后仍可本地 pause/resume/cancel/budget_apply/inspect。
3. Thread 累计 Token 账本、估计结算、后续 adjustment。
4. 硬预算在途 interrupt 和最大风险暴露。
5. 活动 Lease 调用现有 `jobs.leases.renew`。
6. 从锁定协议验证 `optOutNotificationMethods`，关闭不必要增量通知。
7. 回调内数值计算转 Job。
8. 抽公共 P0 Service API，移除 `roots.py` 对 TeamService 私有方法调用。
9. rollout 文件读取不再是正常计量或启动硬依赖。
10. 消费 Codex rate-limit 更新，不自建 RPM/TPM。

### Token/Budget 语义
- Thread 累计 `totalTokens` 为总量主账，Turn 分摊为辅助账。
- Token Unit 与 Provider Money Cost 分开。
- 缺最终回执经过 grace period 自动估计结算，标记 ESTIMATED。
- 后续可信累计数据追加 adjustment，不双重计费。
- 正常流程不要求人工结算。
- 同一 Thread 原则上可继续；不能把旧增量错误分配给新 Turn。
- 硬预算监控累计总量和在途风险；触阈值请求 interrupt。
- 说明通知延迟带来的最大超支风险，不能宣称绝对零超支。
- 若协议不能保证完整累计数据，设计保守估计与可恢复对账，不把私有 rollout 文件作为必需依赖。

## 3. Phase 2 — Workflow Behavior Hooks

复用现有 WorkflowRegistry，不创建第二套 Registry。

定义可信 Workflow Package Hook：
- `validate_intent`
- `build_worker_tools`
- `prepare_dispatch`
- `build_task_context`
- `reevaluate`
- `replan`
- `prepare_job`

签名由真实调用点决定。移除核心对具体 Workflow 的 import 和 if 分支。新增可信 Workflow 不应修改核心 Driver、Roots、Intent、Scheduler、Job Manager。

Hook 必须版本 pin、可信安装并受现有 Governance 管理。缺失 Hook 有明确默认行为或拒绝，不能静默跳过。

## 4. Phase 3 — Agent Generic Compute

复用现有：
- `gx_job_submit`
- `gx_job_status`（按需查询，不循环轮询）
- `gx_job_cancel`
- `gx_capability_inspect`
- `gx_wait_job`
- Artifact 工具

只增加一个受控通用 Artifact Analysis Job：
- Agent 提交脚本和 Artifact refs；
- 只读挂载指定 Artifact；
- 无网络；
- 不暴露主机凭据、GX DB、OAuth、API Key；
- CPU、内存、磁盘、wall time、输出大小限制；
- Python 依赖白名单；
- 通过现有 Job Admission/Budget；
- 输出经 Artifact Service + Execution Manifest 登记。

这不是开放 Shell。必须测试网络访问、路径穿越、沙箱逃逸和资源耗尽。

## 5. Phase 4 — Minimal Team + Multi-Run

Team 仅实现：
- Task `team_id` 标签；
- Team 子预算（复用 `budgets.open_child`）；
- Team `max_parallel_roots`。

不启用旧 TeamManager 的 Leader、Member Presence、Grant、Message Bus 等完整语义。

一个 Workspace 支持多个并行 Run，每个 Run pin 一个 Workflow ID/Version。先确认现有 `run_id` 是否同时承担 Project 和 Workflow Execution 身份；若是，设计最小 additive `run_group` / Workspace-Run 关系，不建设 Project Registry。

## 6. Phase 5 — Runtime Simplification

建立 P0 Dependency Closure：
- P0 必需；
- 兼容性；
- Optional；
- 历史遗留。

先抽公共 Service Interface，再隔离旧 CLI 和 v1 私有依赖。只有有调用方、迁移和恢复证据后才能删除。不得以行数为删除目标。

## 7. Driver / Event Loop

保留单一协调循环和 SQLite 单写者边界。不拆成七个 Worker。

只把阻塞计算移入 Job。回调只记录事件和做轻量验证；不嵌套 App Server RPC。通过锁定协议验证 opt-out 增量通知，保留 Token、Turn、Tool、Rate Limit 和恢复所需事件。

## 8. 并发与 Provider

区分：
- GX Root concurrency；
- Codex per-Root Native Child concurrency；
- Compute Job concurrency；
- Provider rate-limit。

GX 控制 Root 准入和计算资源；Codex 控制原生 Child。不要把 Root 和 Child 计数混为一谈。

不自建 RPM/TPM；消费 Codex rate-limit 更新控制新 Root/Turn 退避。先做多 Root 实测，再决定是否需要复杂调度。

## 9. 验收

必须实际运行：
- 缺失用量回执、估计结算、后续 adjustment；
- 多 Root 硬预算中断；
- 预算耗尽时本地 Operator 操作；
- 活动超过 Lease 默认时长；
- Lease 过期、Attempt 失效、Fencing；
- Job wait 与 Root 容量释放；
- 回调不做长计算；
- 增量通知 opt-out；
- Workflow Hook 完整调用链；
- 新 Workflow 不修改核心；
- Agent 提交通用分析 Job；
- 沙箱无网络/只读/资源限制；
- 两个 Team 的子预算和并发；
- 一个 Workspace 多个并行 Run；
- 旧 DB 冷恢复和 v2 CLI 兼容；
- 2h、8h、24h 分层长程与故障测试。

每项报告代码路径、测试名、命令、退出码和日志路径。区分 Mock、真实 Codex、真实模型、真实计算。

## 10. 部署权限

允许自主创建分支、安装依赖、临时数据库、dev/test/staging 部署、测试和故障注入。

未经明确批准不得覆盖生产数据、执行破坏性生产迁移、取消真实 GPU/Slurm Job、修改生产凭据或自动安装未审核能力。

## 11. 最终交付

- V21_AUDIT_REPORT.md
- V21_REUSE_DELETE_REWRITE.md
- V21_ARCHITECTURE_DECISIONS.md
- V21_IMPLEMENTATION_PLAN.md
- V21_ACCEPTANCE_PLAN.md
- 实际实现代码与迁移
- V21_ACCEPTANCE_REPORT.md
- V21_FAILURE_INJECTION_REPORT.md
- V21_LONG_RUN_REPORT.md
- V21_DEPLOYMENT_REPORT.md
- V21_ROLLBACK_REPORT.md
- V21_KNOWN_LIMITATIONS.md

小步提交；先审计再设计再实施。不得把接口存在、Mock-only 或静态阅读标记为通过。

开始：检查最新 HEAD，运行基线，复现问题，先提交设计，再按 Phase 1–5 自主实施、部署 staging 并验收。
