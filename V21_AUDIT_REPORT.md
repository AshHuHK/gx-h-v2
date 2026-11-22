# v2.1 实施前审计

GitHub main 经指定 Clash 的 HTTPS ls-remote 返回 2314eed72822387e03378e8e676ad5fa9228a8d0。新 checkout 的 HEAD、蓝图基线与之相同。实施目录起初为空。旧部署及旧项目库保持原位。原需求见 [蓝图](GX_H_V2_1_Blueprint/GX_H_V2_1_BLUEPRINT.md)。

| 调用链与原 owner | 复现 / 审计所得 | 修改边界 |
|---|---|---|
| cli.execute → enqueue → Driver.messages → Roots.human_turn | 官方 0.169.0 加本地 Responses。结构化降限多开一个 35-token turn；归零后要求调整额度的结构化 action 留在 pending | 受信本地操作直接进入 Intent/预算事务，模型生成的建议仍走原预览 |
| Driver.drive → Roots.dispatch → RootDelivery._reserve / _ack / _settle → budgets.settle | 临时 SQLite+模拟协议。推进 86,401 秒旧预留仍 HELD；两 Root 报告的用量加既耗已经高于 1M 时，原版还能为第三个 Root 预留 18,928 | 线程累计表与账本调整由同一写事务提交；外部 interrupt 在回调之后 |
| Roots._observe → RootDelivery.observe；jobs.leases.renew | 注入四次真实形状活动通知，3,600 秒 expires_at 未动，随后出现 LeaseExpired 和尝试消耗。已满 rate 窗口未拦住新 turn | 按现有 Attempt/Lease 和活动证明续期；等待、撤销仍归原 owner |
| Roots._call / _submit → TeamService | 6 处使用 5 个私有方法，原有恢复专项 14/14。写产物、裁决、释放及回执处于同一事务 | 抽公共任务提交与回收接口，保留事务 |
| Driver.native → geometry review_tools.recalculate_now | 在工具响应返回前执行完整复算。退出 SQLite 写锁并不能释放原生读取循环 | 外移到受控、幂等 Job，恢复按 Job 和产物引用 |
| p0/cli、driver、intent、replan、orchestration/reactor、service → 具体 workflow | 工作流源码参与已发版 digest，直接换函数会破坏旧 run 恢复 | 复用 WorkflowRegistry，保留旧版精确注册 |
| cli.start → orchestration.service.run_start → budgets.create_root；driver.project | 两行含 p0 标记的 run 在 SQLite 中实测抛出“Use a separate --state directory”。两个 task 同时 claim 已有 BEGIN IMMEDIATE，旧调度只统计单 run | 增加局部 workspace 父预算与 run 关联，按 run_id 路由；保留单一写入协调循环 |
| jobs.tools / capabilities.tools → JobService、registry | 原 Job 入口假定选中的 path，P0 Worker 可无 path。现有 workflow-step 绑定、预算、稳定键与 manifest 可复用 | 对模型只交付受限数据分析语言与已获授权的 Job/Artifact |

原始复现保存在 [预算](acceptance/v21/meter/baseline.json)、[租约与 provider](acceptance/v21/activity/baseline.json)、[操作员入口](acceptance/v21/operator/before.json)。独立基线重新从 Git blobs 导出，命令与完整结果保存在 [baseline](acceptance/v21/baseline/)。上表区分实际复现与源码审计，不将原 v2 的测试记录当作 v2.1 验收。
