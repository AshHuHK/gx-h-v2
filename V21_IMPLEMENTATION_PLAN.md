# v2.1 工作计划

| 批次 | 代码及集成责任 | 出口 |
|---|---|---|
| 1 | 计量 owner、活动与退避 owner、本地操作 owner、公共任务服务 owner；集成负责人连接单循环 | 缺回执估计及调账、硬预算中断、续租与 fencing、零模型控制、锁定协议观测 |
| 2 | 可信 workflow owner 和独立复算 Job owner | 七个 hooks 真实消费、额外包无需改核心、旧 pin 冷开、回调不计算 |
| 3 | 分析 capability owner 复用既有计算与资源 owner | 官方工具入口按授权提交、可取消/等待、无网络、资源可测、manifest 可读 |
| 4 | workspace 资金额度和共享意图 owner、team owner、协调循环由一人集成 | 同库两 run、team 拆分、预算并发隔离、独立暂停与恢复 |
| 5 | 独立验收与 staging owner | 依赖闭包、旧库冷恢复、回退、短时长程实测、准确的未测清单 |

每一模块先记录基线，再由唯一集成 owner 合并重叠事务和入口。所有修改限新 checkout。测试所用目录、代理、发行物和本地依赖与历史运行隔离。

## 已实施的交接

| 阶段 | 实际修改与交接 | 复验证据 |
|---|---|---|
| 1 | `39d97cf` 累计计量与并发风险；`0e40ff5` 活动与退避；`d56e6d0` 本地 Operator；`25a7542` 补齐冷路径累计与历史来源。 | [账本](acceptance/v21/meter/README.md)、[活动](acceptance/v21/activity/results.json)、[本地操作](acceptance/v21/operator/README.md) |
| 2 | `b844b27` 抽取 workflow 行为；`1497a88` 接入真实入口；`a04831a` 把新工作流独立复算交给审查 Job。 | [包验收](acceptance/v21/hooks/README.md)、[真实数值与冷恢复](acceptance/v21/review-job/README.md) |
| 3 | `b004ea7` 提供 Root 可调度的限额分析 Job；`0a73f62` 让合法新 Attempt 复用原 Job 和收费。 | [工具、内核限制、产物](acceptance/v21/compute/README.md) |
| 4 | `00393e0` 实现 workspace 与团队子预算；`4a7b2cb` 接入多 Run 单循环；补测团队暂停、恢复准入和已完成预算归集。 | [多 Run](acceptance/v21/workspace/README.md)、[团队](acceptance/v21/teams/functional.json)、[持续实测](V21_LONG_RUN_REPORT.md) |
| 5 | `8068bb3` 保留旧 CLI；`06789db` 抽取公共提交与失败恢复；`ab09f02` 与 `4845462` 建立旧库迁移、回退与依赖闭包验证。 | [公共服务](acceptance/v21/service/RESULTS.md)、[部署](V21_DEPLOYMENT_REPORT.md)、[回退](V21_ROLLBACK_REPORT.md) |

最终冻结提交、测试汇总和实际未测范围以[集成验收记录](V21_ACCEPTANCE_REPORT.md)为准。
