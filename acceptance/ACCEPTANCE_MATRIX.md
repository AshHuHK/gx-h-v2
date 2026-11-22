# P0 Acceptance Matrix

每项记录：测试 ID、代码位置、命令、输入、预期、实际、日志/Artifact 引用、PASS/FAIL、是否人工介入。

| ID | 验收项 | 必须验证 |
|---|---|---|
| C-01 | App Server handshake | initialize / initialized 顺序、错误处理 |
| C-02 | Thread lifecycle | start / resume / turn start |
| C-03 | Notifications | 不丢 item/turn/thread 通知 |
| C-04 | Native Goal | set/get/clear、token usage、objective replace semantics |
| C-05 | Goal pause/resume | 状态枚举和 Job 等待行为 |
| C-06 | Main Thread Goal disabled | 配置实际生效 |
| C-07 | Steer | 活跃 turn、expectedTurnId、长命令延迟 |
| C-08 | No active turn | 正确 fallback 到 turn/start |
| C-09 | Native Subagent | 原生派生正常，GX 不接管 |
| C-10 | Root Thread restore | App Server restart 后恢复 |
| I-01 | Human authorization | 只有主对话真实人类 turn 可提交 |
| I-02 | Forged authorization | 模型参数/网页/工具输出伪造必须失败 |
| I-03 | Ambiguous input | 只形成 proposal |
| I-04 | Supplement | 不 bump Intent version |
| I-05 | Explicit change | 正确建立 Change Record |
| I-06 | Relaxation confirmation | 放宽硬约束必须后续人类 turn 确认 |
| I-07 | Objective replacement | 必须后续人类 turn 确认 |
| I-08 | Resource increase | 必须后续人类 turn 确认 |
| I-09 | Item-level impact | 只影响真实依赖对象 |
| I-10 | Reuse artifacts | 评估规格变化时复用上游产物 |
| I-11 | Transaction ordering | Intent commit before Codex delivery |
| I-12 | Delivery retry | Codex 通知失败后 Outbox 重试 |
| I-13 | Duplicate delivery | 幂等，不重复产生有效动作 |
| I-14 | Concurrent Job completion | 与 Intent commit 两种事务顺序都自洽 |
| T-01 | Lease fencing | 旧 Attempt 无法正式提交 |
| T-02 | Job outcome unknown | 回执丢失先对账，不盲目重投 |
| T-03 | Job survives Thread | Agent/Thread 退出不影响 Job |
| T-04 | Resource admission | 超预算/禁用能力拒绝准入 |
| E-01 | Execution Manifest | 每个 Job 从开始就记录完整清单 |
| E-02 | Manifest verification | 输入/输出 hash 可验证 |
| E-03 | Frozen Evaluation | Evidence 引用固定评估版本 |
| E-04 | Measurement failure | 不形成支持/反驳 Evidence |
| E-05 | Independent reviewer | Reviewer 与决策/作者分离 |
| E-06 | Four-way settlement | 四类结果正确区分 |
| R-01 | Three research rounds | 连续三轮端到端自主推进 |
| R-02 | Stop | 达到停止条件后不继续烧预算 |
| R-03 | Pivot/prune/continue | 决策正确改变后续路径 |

## Required fault injection

1. GX process killed during SQLite transaction.
2. Codex App Server killed during active turn.
3. App Server killed after `thread/start` accepted but before GX records returned Thread ID.
4. Notification lost after successful request.
5. Job backend accepted submission but response lost.
6. Job completion races with Intent change.
7. Old Thread resumes after Attempt fencing.
8. Duplicate Outbox delivery.
9. Long shell command while steer is sent.
10. Compaction then resume.
11. Unsupported provider / modelProviderMismatch.
12. Resource limit reached while multiple Threads compete.

## Required evidence

- Test output and exit code.
- App Server protocol logs with secrets redacted.
- SQLite state snapshots before/after.
- Outbox rows and delivery attempts.
- Job backend records.
- Execution Manifest and hashes.
- Codex thread/turn IDs.
- Recovery timeline.
- Token/GPU/API cost.
- Final `ACCEPTANCE_REPORT.md`.

Mock-only tests do not count as passing integration tests. If a test cannot be run, mark BLOCKED with exact reason.
