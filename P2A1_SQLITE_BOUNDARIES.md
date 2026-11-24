# P2A.1 SQLite 写事务审阅

**SQLITE-BOUNDARIES 通过 8/8。** 在干净独立工作树 `5bc7c7e89d883f7ea014d3c7ef0180ce5065fd32` 上，5 个 Run 产生 29 条最外层业务及真实入口夹具写事务记录，25 次提交、4 次回滚。每条都记录 owner、operation、实际 Run、获取写锁时间、事务体时间、提交或回滚时间及行数。两个并发 Reactor 另有 2 条写记录。数据来自文件数据库与脚本输入，证据类别为 `SCRIPTED`。[专项结果](acceptance/p2a1/sqlite-5bc7c7e/result.json) · [逐笔遥测](acceptance/p2a1/sqlite-5bc7c7e/transactions.jsonl) · [冻结版本与复现命令](acceptance/p2a1/sqlite-5bc7c7e/receipt.json)

从上次签收提交 `61ea96d` 比较 27 个有变动的产品或示例源码文件。Python 语法树比较列出 13 处新增或改变的事务边界、17 处新增或改变的直接写 SQL。审阅同时回溯了 SQL 辅助函数、调用者持有的写锁和安装迁移。新建 Run 的身份在首次提交前从实际返回值绑定，幂等重放取出同一 Run。独立数据库的两次公开 `gx_run_start` 留下相同的身份。[带路径和行号的差异清单](acceptance/p2a1/sqlite-5bc7c7e/source-audit.json) · [写事务与幂等启动](gx-h/gx_core/persistence/store.py)

| 业务路径 | 写锁内的操作 | 在写锁外检查的操作 |
|---|---|---|
| FIFO | 既有外层事务提交后，Store 才调用本机通知。FIFO 代码操作本机文件描述符。 | 50 次文件系统通知均在写锁释放后。读取 `data_version` 的等待器仅查询连接。见 [Store](gx-h/gx_core/persistence/store.py)、[FIFO](gx-h/gx_core/jobs/wakeup.py) 、[Waiter](gx-h/gx_core/p0/idle.py) 和 [FIFO 冻结复测](acceptance/p2a1/fifo/descriptor-errors/raw.tar.gz)。 |
| CPU Job 准入 | `JobService.admit` 核对当前开放的 CPU 占用、最早可行等待者及预算，在同一事务登记或拒绝，其他连接新进入的占用按已占用处理。 | 6 次准入采样、13 次 CPU 采样和 10 次 Job 状态读取均在写锁外。5 次来源文件取证和 5 次解释器运行环境取证也在写锁外。见 [Job 准入](gx-h/gx_core/jobs/service.py)、[容量实测](gx-h/gx_core/jobs/capacity.py) 和 [执行清单](gx-h/gx_core/p0/manifest.py)。 |
| 停放和恢复 | Engine 的 `checkpoint_node` 在当前 checkpoint 事务内增删本节点等待位置。`Engine.resume` 和 `Reactor.drive_science` 在写锁内复核可执行性。 | 真实两槽均被占用时，等待节点恢复被拒绝；独立节点已经推进。两个 Reactor 同时看到可推进节点，只有一个连接推进该节点。两次提交改变 4、0 行，第二个连接等待写锁 8.229 ms。见 [Engine](gx-h/gx_core/orchestration/engine.py)、[队列索引](gx-h/gx_core/jobs/cpu_queue.py)、[Reactor](gx-h/gx_core/orchestration/reactor.py) 和 [并发遥测](acceptance/p2a1/sqlite-5bc7c7e/supplement/transactions.jsonl)。 |
| Operator 与用量 | `Operator.submit/apply/reject` 写入命令及处理回执。`Usage.maintain`、`Usage.remember_official_history` 和 `RootDelivery.verify_descendant` 使用本 Run。`Budget.record_released_native_usage` 在 Operator 事务中使用 SAVEPOINT，随外层一起提交。 | Operator 对实际数据的 3 次视图读取、4 次费用分类、1 次已绑定历史准备均在写锁外。源文件解析先完成，Writer 复核具体预留与证据。只读 SQLite 连接成功生成同一 Run 的费用报告。见 [Operator](gx-h/gx_core/p0/operator.py)、[人工解决](gx-h/gx_core/p0/operator_usage.py)、[计量报告](gx-h/gx_core/p0/metering_report.py) 和 [持久账本](gx-h/gx_core/p0/metering.py)。 |
| Run 资源记录 | `Run.run_start`、`Capability.create_probe_run`、示例 `P01Fixture.prepare_run` 将资源条件随新 Run 首次写入。持有者传入真实 Run，或者显式传入提交前填入实际 Run 的容器。 | 从公开入口创建并重放，运行资源采样 3 次，均在写锁外。直接创建行的代码也复用连接打开前采集的本进程资源快照。见 [Run 创建](gx-h/gx_core/orchestration/service.py)、[采集](gx-h/gx_core/jobs/run_resources.py)、[低层行创建](gx-h/gx_core/persistence/records.py) 和 [能力探针](gx-h/gx_core/capabilities/ladder.py)。 |

`p0_usage_releases`、`p0_usage_host_histories` 及旧账的 `unresolved_at` 补录使用既有 P0 schema owner。安装时涉及整个工作区，尚无对应 Run；首次遇到 CPU 竞争时创建的队列索引属于该节点原有的 Engine 写事务。取证区分风险报告使用的只读快照、全库 schema 安装、临时夹具建数和单 Run 写入。[计量迁移](gx-h/gx_core/p0/zzzzzzz_p2a1_usage_resolution.sql) · [P0 schema owner](gx-h/gx_core/p0/schema.py) · [带标签的实测边界](acceptance/p2a1/sqlite-5bc7c7e/result.json)

后续 `c369cd6` 扩大旧账未归属项目的查询范围。该版本独立工作树再跑同一专项，8/8 通过，记录 29 条写事务，受监测的重操作在 writer 内调用次数为 0。[后续版本实测](acceptance/p2a1/sqlite-5bc7c7e/followup-c369cd6/result.json) · [该版本源码清单](acceptance/p2a1/sqlite-5bc7c7e/followup-c369cd6/source-audit.json)

## 修复记录与测量范围

最初的公平性草稿在 writer 内解读所有可执行 Run 的最新 checkpoint。每个 checkpoint 含另外 12 个分支和约 2 KiB 上下文，1、100、400 个等待 Run 分别返回 1、100、400 个队列元素，5 次读的中位数为 0.037、1.768、7.229 ms。最终实现对最早符合条件的等待项取单行。相同规模下中位数为 0.033、0.033、0.034 ms，SQLite 的实际读表记录只有 `job_cpu_waits`、`runs` 和 `sqlite_master`。本组中队首可行。[原草稿测量](acceptance/p2a1/sqlite-5bc7c7e/before-fix/queue-scaling.json) · [修复后测量与读表记录](acceptance/p2a1/sqlite-5bc7c7e/result.json)

最初独立复测还捕获了有效的 `accept-exposure` 后显式释放失败。Budget 将首次记账的空 previous 视为非法，Operator 的 apply 事务回滚。修复后有效释放提交，过期接受事件和不存在的宿主记录仍被拒绝。早期结果来自开发中的工作区，在回执中单独标明。Job 来源及解释器哈希移到准入事务之前，CPU 等待节点恢复时也先采样、再在写锁内复核。[原失败](acceptance/p2a1/sqlite-5bc7c7e/before-fix/operator-result.json) · [原写记录](acceptance/p2a1/sqlite-5bc7c7e/before-fix/operator-transactions.jsonl) · [修复后汇总](acceptance/p2a1/sqlite-5bc7c7e/summary.json)

29 次事务体的 p50、p95、最大值依次为 0.143、8.561、46.296 ms。最慢样本属于 `Engine.checkpoint_node`，改动 6 行；本次写锁等待最大值为 0.008 ms。多连接竞争的单独 Reactor 样本见上表。旧版可选输入 Artifact 的复制路径另作实测，公开 `gx_run_start` 接受一个 1 MiB 文件时，仍在 `Run.run_start` 内各执行一次文件读取和 Artifact 发布，事务体耗时 6.175 ms。该旧路径仍在现有存储契约内；本次资源标记关注 CPU、内存及设备观测。队列规模实验、旧输入复制实验和冻结主样本各自保留原始量值。[最慢样本](acceptance/p2a1/sqlite-5bc7c7e/summary.json) · [输入复制观测](acceptance/p2a1/sqlite-5bc7c7e/supplement/result.json) · [输入复制源码](gx-h/gx_core/orchestration/service.py)

## Phase 2B 的图变更准入条件

后续引入图编辑时，先在同一 SQLite **只读快照**中读出原图版本、节点、边、受影响的运行状态和会用于决策的修订号。在写锁外完成图验证、环检测、依赖影响范围及计划生成。计划携带原版本、关联修订和预期写集。

持有者随后开启一个短写事务，按原版本与相关修订执行条件更新。匹配后在同一事务登记所需的新版本、受影响记录和持久事件，再提交。条件不匹配时回滚本次计划，在新的只读快照上重新计算并重试。并发验收需让两个计划基于同一旧版本，核对至多一个原计划提交、另一计划重算、事件与状态一致，并用既有遥测分别量出规划时间和写锁持有时间。以上是下一阶段的设计与验收条件；当前数据只覆盖表中列出的 P2A.1 路径。

可从发行目录运行本专项，公开入口会封装归档及清单。冻结专项原件和补充实验也有独立 SHA-256 清单。[门槛实现](acceptance/p2a1/sqlite_boundaries.py) · [原件清单](acceptance/p2a1/sqlite-5bc7c7e/manifest.sha256)

```sh
bin/gx-acceptance run hardening --case SQLITE-BOUNDARIES
```
