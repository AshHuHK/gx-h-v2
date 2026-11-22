# GX-H v2.1 验收记录

产品入口为 `bin/gx-p0`。比较基线是 v2 的 `2314eed72822387e03378e8e676ad5fa9228a8d0`。本轮使用正式 Codex App Server 0.169.0、SQLite 和本地真实 CPU Job。协议测试中的模型由环回 Responses 服务驱动，可以精确重放少见事件次序。数值分析与独立复算在 CPU 上实际运行。本机现有 ChatGPT OAuth 额外完成一次 gpt-6.2-astra / xhigh 真实推理，耗时 7.137 秒，官方回报 16,206 token。此前 v2 的真实模型科研记录沿用其原版本归档。[OAuth 原生回执](acceptance/v21/deployment/oauth-smoke.json)

## 部署和长程数据

- 交付的产品提交为 `a05dcef64522fbbbc12120b1d78da518e6886b10`。最后一个修复允许已写入初始 Intent、尚无首份 checkpoint 的旧 Run 按原 ID 恢复，即使同一 workspace 已有另一个带 key 的 Run。[队列及混合升级回归](gx-h/tests/test_v21_workspace_queue.py)
- 16 分 2.729 秒的独立候选实测完成两个 Run、两个 Team、36 轮、72 个 Root 提交和 38 个 CPU Job；所有 Job 第一次尝试成功，114 次活动续租。独立核对 80 个坐标并验证 530 份 Artifact 的哈希。进程退出至下一份科学决策耗时 43.813 秒。[墙钟记录](acceptance/v21/long-run/long.json)
- 两轮 2RE9 描述性几何研究使用固定 `tl1a_geometry@3`，六个真实 CPU Job，独立审查员各提交一份含数值差异、容差和证据引用的复算结果。已检查 25 份科学产物和两套执行清单。[独立复算](acceptance/v21/review-job/public-cli.json)
- 旧库的迁移和回退用 v2 原提交创建、原二进制运行的在途项目制作备份，GX 和 Codex 的状态一并存档。替换软件、恢复原库、退回 v2，再升级至 v2.1 并重复开启终态都在 staging 内执行。[备份与回退](V21_ROLLBACK_REPORT.md)

在最终提交的独立 Git 归档上，两队各三轮的复验实际运行 **80.544 秒**，8/8 CPU Job 首次成功，12 个原生 turn 确认结账。进程退出后 17.720 秒提交下一项决策；终态重开新增模型请求为 0，22 张业务表逐表完整摘要一致。[最终提交的运行记录](acceptance/v21/long-run/released.json)

基线 Python 回归为 273 通过、4 跳过。最终提交的独立环境中，Python 全量 344 项，340 通过、4 项可选 DSH 跳过；Node 插件测试 2/2 通过，生成配置一致性检查通过。[基线清单](acceptance/v21/baseline/summary.json) · [冻结版本 Python 日志](acceptance/v21/final-a05dcef/python-full.log) · [Node](acceptance/v21/final-a05dcef/node.log) · [生成检查](acceptance/v21/final-a05dcef/generator.log)

**协议探针的首轮读数。** 最终冻结产品的 17 个原版官方协议场景均通过。第一遍专项测试另有两条原断言未满足，原始日志已保留。双 Root 用量有时先到达 `rawResponse/completed`，先前探针只在 token 通知时记录触限现场。Job 唤醒测试在官方 ACK 之后、GX 保存 ACK 之前杀进程，原断言在三秒时要求准确加 35，新建项目的默认对账宽限为 30 秒。三秒时已核对同一官方 turn 且没有第二次模型请求，2000 token 的预留尚在。超过真实宽限后，在暂停状态连续两次冷恢复，只结算一个 wake 估值 2000，已知下界 35。科学执行与费用置信度可分别核对。[首次结果](acceptance/v21/final-a05dcef/summary.json) · [30 秒后两次冷恢复](acceptance/v21/protocol/supplement/wake-paused.json)

**最终协议结果。** 在同一份产品代码上补跑五条官方协议探针，五条全部退出 0。连同已通过的用例共 17 个固定协议场景、13 个 v2.1 专项场景。两次归档中 423 个产品、脚本及测试文件的聚合 SHA-256 均为 `2101a99e9fc48e2c4c3c680c211413aeb2d91ef033b85001a6b9c8020bed63c4`。该唤醒只产生一次有效继续和一次结算。旧 Attempt 已释放，后续请求被拒，本例没有取得可归属的最终精确费用。另一个仍合法持有原线程的用例取得了后继累计 70，触发一次 −65 更正，冷重放未增加调用。[统一结果与重跑命令](acceptance/v21/final-a05dcef/README.md) · [机器可读汇总](acceptance/v21/final-a05dcef/aggregate.json) · [可归属与不可归属的对照](acceptance/v21/meter/README.md)

## 业务验收矩阵

| 蓝图编号 | 行为与验收观察 | 主要实现 | 测试及原始记录 |
|---|---|---|---|
| A01、A02、A06 | 按 thread 累计计数，cached input 在总量中计一次；重复、倒退及重开不能重复扣费。 | `p0/metering.py`、`p0/root_delivery.py` | [账本边界用例](gx-h/tests/test_v21_metering.py)、[官方累计值与冷恢复](acceptance/v21/protocol/cumulative-cold.json) |
| A03、A04、A05、A10 | 已收到终态而计量不全的 turn 经过宽限后可估算结算；后续累计值修正 thread 总账，保留估计来源和无法确定的单 turn 分摊。缺失投递 ACK 仍交给投递恢复。 | `p0/z_v21_metering.sql`、`p0/metering.py` | [warm](acceptance/v21/protocol/cumulative-warm.json)、[cold](acceptance/v21/protocol/cumulative-cold.json) |
| A07、A08 | 预算 100、两笔 40 的预留，观测 30 与 73 时，两条在途 turn 均收到中断并核对终态，第三次付费启动被拒。 | `p0/meter_limits.py`、`p0/root_delivery.py` | [真实双 Root 协议探针](acceptance/v21/protocol/budget.json) |
| A09 | 预算耗尽、native 锁被持有时，仍可本地暂停、取消、调整额度、对账。已展示的预览绑定具体修改，重复提交具有同一结果。 | `p0/operator.py`、`p0/operator_budget.py`、`budgets/allotment.py` | [CLI 前后对照](acceptance/v21/operator/after.json)、[越权、重放与并发边界](gx-h/tests/test_p0_local_operator.py) |
| B01、B02、B03 | 活动续租核对运行中的 turn、当前 Attempt、租约以及 fencing。真正失效的 Attempt 按原失败预算处理，旧 Root 无法写入。 | `p0/activity.py`、`teams/service.py`、`p0/roots.py` | [官方活动租约](acceptance/v21/protocol/lease.json)、[时间边界](gx-h/tests/test_v21_activity.py)、[真实失败路径](gx-h/tests/test_p0_service.py) |
| B04、B06、B07 | Job 等待释放 Root 容量，恢复时认领原任务、原 Job 和正确回执；暂停期间越权读取被拒，恢复后同一 Attempt 可读取同一 Artifact。 | `p0/waits.py`、`p0/driver.py`、`p0/team_admission.py` | [暂停时已在途的读取](acceptance/v21/long-run/pause-read.json)、[进程退出及冷启动](V21_FAILURE_INJECTION_REPORT.md) |
| B05 | provider 退避与科研失败次数独立记录，恢复同一事件，在途和冷启动均复核官方状态。 | `p0/activity.py`、`p0/root_delivery.py` | [官方拒绝探针](acceptance/v21/native_refusal.py)、[端到端 HTTP 429 注入](V21_FAILURE_INJECTION_REPORT.md) |
| C01、C06、C07 | 每 workspace 单协调循环，SQLite 事务与 Outbox 顺序可恢复；度量回调、SQLite 和 dispatch，用实际冷重放核对收据与工作量。 | `p0/coordinator.py`、`p0/driver.py`、`teams/service.py` | [同一提交、verdict、Outbox 重放](acceptance/v21/service/after.json)、[并行运行与延迟分布](V21_LONG_RUN_REPORT.md) |
| C02、C03 | initialize 关闭冗余增量流，并保留 turn、tool、计量和限额通知。 | `p0/roots.py`、`p0/activity.py` | [真实 App Server 的订阅效果](acceptance/v21/protocol/routing.json) |
| C04、C05 | 新 2RE9 工作流把独立复算交给审查 Job。请求、等待、恢复、取回分别有正式记录，工具回调不执行拟合也不嵌套调用 App Server。 | `workflows/tl1a_geometry_v3`、`adapters/scientific/tl1a_geometry/review_batch.py` | [CPU 产物、回调计时和冷断点](acceptance/v21/review-job/README.md) |
| D01–D05 | 安装包提供 Intent 校验、worker 工具、派发准备、任务上下文和复评、重规划、Job 准备行为；同一份数据按新的正式判据得到更新后的评估。 | `orchestration/hooks.py`、`orchestration/registry.py`、`orchestration/packages.py` | [外部 calibration 包](acceptance/v21/hooks/calibration_package.py)、[执行轨迹](acceptance/v21/hooks/run.json) |
| D06–D08 | 缺省行为和缺失实现的拒绝路径、仅改安装清单加载新工作流、老研究使用原图 pin。 | `config/workflow_packages.json`、`workflows/compat_v20` | [新包公开入口](acceptance/v21/hooks/run.py)、[读取旧项目精确版本](acceptance/v21/hooks/old-pin.json) |
| E01–E05 | Root 可查看能力，提交已准入 Job，停车等待，取消自己获准的 Job 并取回结果。 | `p0/compute.py`、`jobs/service.py`、`jobs/tools.py` | [官方 Root 工具轨迹](acceptance/v21/compute/official.json) |
| E06–E10 | 有限表达式仅取得获准 Artifact 的输入字节；独立进程限制文件、网络、CPU、内存和输出，结果登记 Artifact 与 Manifest；失效租约无法提交。 | `adapters/local/artifact_analysis`、`p0/compute.py`、`p0/manifest.py` | [实际计算及限制](acceptance/v21/compute/official.json)、[资源耗尽与作用域边界](gx-h/tests/test_v21_compute.py) |
| F01、F02 | 一个项目内多个 Run 各自固定工作流及独立任务、Job、预算；项目 Intent 与 Run 自身的研究问题区分记录。 | `p0/workspace.py`、`p0/coordinator.py`、`orchestration/service.py` | [公开启动](acceptance/v21/workspace/public-start.json)、[实账](acceptance/v21/workspace/accounting.json)、[共享 Intent](acceptance/v21/workspace/intent.json) |
| F03–F08 | Task 带 team 标签，沿父子预算扣账，team/run/workspace 容量在领取时原子检查，暂停某组后其余组可继续。原生 child 由 Codex 管理。 | `p0/team_admission.py`、`p0/workspace.py` | [标签、容量、暂停、Job 和结账](acceptance/v21/teams/functional.json)、[旧管理器调用观测](V21_DEPENDENCY_CLOSURE.md) |
| G01–G03 | 从指定 v2 的在途项目恢复；兼容 CLI 保留，P0 通过公共 TeamService 处理提交和真实失败，保留原事务。 | `cli/compat.py`、`teams/service.py`、`p0/roots.py` | [原库迁移及回退](V21_ROLLBACK_REPORT.md)、[基线与候选实际行为](acceptance/v21/service/after.json) |
| G04–G07 | 验证能力准入及原生 child、App Server、固定协议、缺失 rollout 时公开线程累计及重复 wake。 | `capabilities/`、`p0/root_delivery.py`、`hostobs/` | [官方探针](acceptance/v21/protocol/)、[无 rollout 的冷对账](acceptance/v21/protocol/cumulative-cold.json)、[治理回归](acceptance/v21/teams/governance-regression.log) |
| G08 | 依赖从入口导入和实际 staging trace 双向核对，覆盖兼容用到的模块、离线材料和 CPU 外部进程。 | `acceptance/v21_closure.py` | [依赖闭包与证据](V21_DEPENDENCY_CLOSURE.md) |

表内简写的产品路径分别位于 `gx-h/gx_core` 或 `gx-h`。每组探针及测试可从相应证据目录或[完整回归驱动](acceptance/v21/regression.py)复跑。部署、回退、限时运行和未验证边界分别记录在[部署报告](V21_DEPLOYMENT_REPORT.md)、[回退报告](V21_ROLLBACK_REPORT.md)、[长程报告](V21_LONG_RUN_REPORT.md)和[已知限制](V21_KNOWN_LIMITATIONS.md)。
