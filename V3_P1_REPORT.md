# P1 网格科研插件与派发

操作员在项目 JSON 的顶层加上 `"plugins": ["org.gx.science-grid@2.0.0"]`，选择该项目的插件。公开入口 `bin/gx-p0 start --state DIR --run-key KEY --spec FILE` 将所选包、工作流和能力回执随 Run 创建一同提交。可以用 `bin/gx-p0 status --state DIR --run-id RID` 查看记录。

## 科研调用路径

| 所有者 | 本次实现 |
|---|---|
| 插件包 | `register(ctx)` 引用原有 `science_grid@2`，提供项目启动输入、科学任务上下文和可供 Root 调用的 `gx_grid_measurement`。工具按当前任务的路线 ID 读取真实 Job、原始观测与冻结判据，返回观察数量、最大值和通过阈值的数量。[插件实现](gx-h/plugins/installed/org.gx.science-grid/2.0.0/plugin.py) |
| Workflow Registry | Run 继续使用原图、评审产物校验与完成判定。原图 SHA-256 为 `4f922e187a641e36c679e2a78879ce81dec0b687342bd14779293c7771b4f467`。各 Run 的额外钩子与工具由所选包构造。[Run 绑定](gx-h/gx_core/plugins/runtime.py) · [对照图谱](V3_MIGRATION_MAP.md) |
| Capability Registry / Job | 插件声明 `fixture-grid@1`。能力注册和准入仍使用原方法的清单及治理状态；可移植摘要 `b69193d4546b42225261484872718eb475478c5836377de6cacd6b8c38b9f12f` 和当前工作区实际产物引用都写入回执。执行、记账、取消和 Artifact 保留各自原有所有者。[能力附件](gx-h/gx_core/capabilities/shipped.py) |
| Root 与派发 | 对新任务依次检查依赖、Provider、团队和调度容量，再执行 `prepare_dispatch`。需要重新评分或补审时，释放领取事务进行准备，重新确认任务和科学输入后才签发租约。受到阻塞的任务按任务和尝试退避。[领取逻辑](gx-h/gx_core/p0/roots.py) · [数值工作流的准备与提交](gx-h/workflows/compat_v20/grid_dispatch.py) |
| 通用科研记录 | Driver、Impact 和 Root 通过共享科研记录访问已有的人类来源、已接受的提交、尝试和决策。兼容回放仍保留最初固定的图和计算代码。[科研记录](gx-h/gx_core/research/records.py) · [新旧投影实测](acceptance/v3/p1/grid-projection-parity.json) |

## 部署和回退

只在独立 v3 安装和测试数据副本中运行。旧 v2.1 的项目不携带插件选择，打开时沿用原工作流。已有 v3 项目续跑需要原插件摘要、方法回执和工作流版本。恢复时可以对照公开状态中的 `plugin_binding`。原 v2.1 数据副本的回退步骤见 [冷恢复操作](acceptance/v3/staging/README.md)。

## 冻结提交与离线签收

首次 P1 候选为 `336225603dec3a1bc5efd6eccb401ccd25e63287`。下表为该 Git 树的独立测试记录。名额释放后的等待修正提交为 `2476f2bdd74b53e3833ce35d08287120e4fdf0c8`。

| 检查 | 结果 |
|---|---|
| 真实 CPU Job，脚本驱动的官方 Codex 会话 | v2.1 原版、v2.1 冷升级、P1 兼容模式和 P1 插件模式四组得到了相同的科研输出摘要 `76d1e21e47f8b6207cd57ab911f0ed40bb0538f72c77668e2b7a6cc1ff71f095`。每组完成 4 个 Job、6 个审核或决策提交，核对 58 个 Artifact，终态重放新增请求为 0。插件模式的 Root 根据两次工具调用读到的值提交了获接受的审核。耗时 69.699 秒，退出码 0。[四组结果和原始命令](acceptance/v3/p1/paired-grid-frozen.json) · [函数调用回执](acceptance/v3/p1/paired-grid-traces/index.json) |
| 旧项目在途恢复 | 复制既有暂停项目、原产物、3 组原始 Job 回执与 Codex 状态。可写打开和回退均通过，续跑总计 4 个 CPU Job，3 项决策获接受。原 Job 回执一致，剩余预留为 0，终态重放新请求为 0，退出码 0。[冷恢复及回退](acceptance/v3/p1/legacy-checkpoint.json) |
| 租约、计量、任务与评分 | 覆盖容量拒绝、准备阶段竞争、Node 评分时数据库事务状态、领取前进程退出、冷恢复复用原 Job 与原评分以及科学输入并发更新。原有与新增派发用例共 67 项通过。[测试日志](acceptance/v3/p1/dispatch-tests.log) |
| 原平台完整回归 | 281.968 秒，退出码 0。Python 发现 382 项，通过 378 项，4 项沿用原有 DSH 跳过条件。124 项 P0 子集、2 项 Node、生成器及 30 个官方 Codex 环回场景都通过。先前签收的 16 项 v3 P0 测试逐一通过。[环境与命令](acceptance/v3/p1/regression/invocation.json) · [结果](acceptance/v3/p1/regression/summary.json) · [原用例核对](acceptance/v3/p1/regression/frozen-comparison.json) · [P0 身份核对](acceptance/v3/p1/regression/p0-case-comparison.json) |

同负载真实模型对照于 2026-11-22 19:57:24 UTC 开始，双方均使用 OpenAI OAuth、`gpt-6.2-astra`、`xhigh` 和原生 Codex 0.169.0。[对照口径](acceptance/v3/performance/PROTOCOL.md)

### 对照中发现并重测的等待延迟

早期候选 `3362256` 的两波实测显示，上一名 worker 已完成计量并释放团队名额后，后继任务的领取中位等待从 119 ms 增至 899 ms，p95 从 128 ms 增至 2,019 ms。队列的退避定时器在名额已经可用时仍然有效。采集在第 2 波全部结算后的空闲边界停止，账本保留 4 个 Run、8 个成功 Job、12 个已对账的 turn 和 1,330,448 tokens。两波的付费数据单独列入累计开销。[停止回执](acceptance/v3/performance/p1-prior-3362256/partial-stop.json) · [同负载环回对照](acceptance/v3/performance/dispatch-control/REPORT.md)

对照还发现，原平台的大量容量拒绝在几毫秒内返回，会拉低所有派发调用合在一起的 p95。用相同 Git 版本、数值输入和领取状态再分别测量，P1 的合格领取 p95 从 7.250 ms 变为 8.330 ms；容量拒绝从 1.873 ms 降至 0.811 ms。每组每类 128 次，无外部模型调用。报告同时保留总分布和按状态分组的结果。[本地领取分组测量](acceptance/v3/p1/dispatch-comparison/REPORT.md)


### 名额可用时及时唤醒

修正后保留退避，同时观察上次实际占用名额的 Root 或 Provider 回执。前一项工作真正释放资源时，后继任务可以在下一遍调度领取。历史已释放的 Root、无关团队和仍有计费 turn 的租约不会提前唤醒任务。

在冻结修正版本上，70 项派发、团队、计量等待和补充证据测试通过。每类 512 次稳定等待观测均无写入。团队和 Provider 两个分组的查询 p95 分别为 17.82 µs、7.32 µs。每次仅按索引读取此前记录的实际占用者。[查询计划、原始样本和命令](acceptance/v3/p1/dispatch-release-watch/REPORT.md)

使用相同插件、相同输入和相同本地 Responses 进行交错的八次对照，名额实际开放至领取的中位数/p95 从 1,826/2,965 ms 降至 127/531 ms。每组均完成 8 个 Run、16 个 CPU Job 和 24 个 turn。[交错对照](acceptance/v3/performance/dispatch-control-2476f2b/operations.json)

修正版本的独立全量回归耗时 287.070 秒，退出码 0。385 项 Python 测试中 381 项通过、4 项沿用基线跳过，124 项 P0 子集、2 项 Node 测试、生成器和全部 30 个固定 Codex 环回场景通过。另从该提交复跑四条科研路径，89.410 秒完成，科学结论、原始测量和冻结评分与前一提交一致，每组终态重放均无新增请求。[回归核对](acceptance/v3/p1/regression-2476f2b/frozen-comparison.json) · [四组科研数据](acceptance/v3/p1/paired-grid-2476f2b.json) · [独立数值对照](acceptance/v3/p1/paired-grid-2476f2b-traces/index.json)
