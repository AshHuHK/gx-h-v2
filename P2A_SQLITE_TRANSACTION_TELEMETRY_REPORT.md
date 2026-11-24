# P2A SQLite 写事务遥测

**DB01、DB02 通过。** 同一个 `Driver.enqueue_message` 在两个独立 SQLite 连接上竞争写锁。首次运行中，持锁方在事务体内耗时 150.298 ms，等待方获取写锁耗时 178.703 ms。其各自另一阶段分别为 0.010 和 0.400 ms。二次运行得到相同归因。证据为 `SCRIPTED`，使用文件数据库、真实 WAL 写锁和脚本化消息。两次运行各自保留数据库、六条原始采样和完整统计。[冻结源码回执](acceptance/closure/sqlite/4e2436e/receipt.json) · [原始归档](acceptance/closure/sqlite/4e2436e/raw.tar.gz) · [SHA-256 清单](acceptance/closure/sqlite/4e2436e/manifest.sha256)

## 测量边界

- 业务写入口显式传入 `transaction_owner`、常量 `operation` 和所属 `run_id`。一次物理写锁产生一条记录。嵌套 SAVEPOINT 的工作和回滚计入持锁方。首次建库、P0 schema 安装和幂等记录由 Store 单独标记。没有 Run 上下文的全库操作允许 `run_id` 为空。[事务边界](gx-h/gx_core/persistence/store.py) · [P0 schema](gx-h/gx_core/p0/schema.py)
- `lock_wait_ms` 测量调用 `BEGIN IMMEDIATE` 至返回的单调时钟耗时。它包含取得写锁所需的 SQLite 和系统调度时间。`body_ms` 从取得写锁计至业务块正常或异常退出。`finish_ms` 计量随后的 COMMIT 或 ROLLBACK，包含提交约束失败后执行的回滚。`duration_ms` 为三段总和，数据库结束后的本地回调耗时排除在外。历史字段 `lock_acquire_ms` 继续表示与 `lock_wait_ms` 相同的数值。[写事务实现](gx-h/gx_core/persistence/store.py)
- `commit_or_rollback` 记录 `commit`、`rollback` 或 `begin_failed`。失败的 BEGIN 保留等待时间，事务体、结束和行变更为零。`rows_changed` 取 SQLite 连接的 `total_changes` 增量，扣除被 GX 内层 SAVEPOINT 回滚的变更。最外层回滚返回零。同一行经过多次 SQL 更新时会多次计数。[事务计数](gx-h/gx_core/persistence/store.py)
- 配置 `GX_METRICS_FILE` 开启采样，沿用原有每次最外层事务一次追加的写入方式。计时和行变更读取在内存中完成，文件写入在当前连接的写锁结束后进行。样本包含身份字段、操作标签、计数和时间；私有输入、SQL 文本和异常正文不进入事务记录。[现有指标出口](gx-h/gx_core/metrics.py)

## 业务入口与慢事务

六个已有用例在冻结源码 `4e2436e219498e6b6f7571fde2cbf0ec844db1ee` 的独立导出上通过。测试覆盖双等待恢复、Driver 消息交接、Root 交付、Usage 宽限期、实际本地测试 Job 的恢复以及审阅期间的 Outbox 和 Reactor。同一跟踪记录含 214 个物理事务，204 次提交、10 次回滚。总耗时最慢的五条依次来自 Job 准入、Engine 节点执行、两次 Store 初始化 P0 schema 和另一次 Job 准入。最大值为 `JobService.admit` 的 43.657 ms，其中事务体占 43.174 ms。[业务入口与命令回执](acceptance/closure/sqlite/4e2436e/receipt.json) · [逐条样本及命令](acceptance/closure/sqlite/4e2436e/raw.tar.gz)

| 已测事务 | 总时长 p50 / p95 / max，ms | 获取写锁 p50 / p95 / max，ms | 事务体 p50 / p95 / max，ms | 提交或回滚 p50 / p95 / max，ms |
|---|---:|---:|---:|---:|
| 214 次业务夹具调用 | 0.623 / 29.018 / 43.657 | 0.007 / 0.038 / 0.079 | 0.379 / 27.398 / 43.174 | 0.247 / 1.113 / 7.359 |
| 6 次首次受控写锁尝试 | 1.370 / 179.697 / 179.697 | 0.018 / 178.703 / 178.703 | 0.142 / 150.298 / 150.298 | 0.045 / 0.594 / 0.594 |
| 6 次重复受控写锁尝试 | 1.340 / 179.891 / 179.891 | 0.047 / 178.806 / 178.806 | 0.258 / 150.394 / 150.394 | 0.056 / 0.641 / 0.641 |

已测业务样本按入口分布为 Engine 34、Reactor 11、Driver 5、Outbox 1、Usage 3、JobService 32、RootDelivery 13、Store 14。该冻结样本还包括其他调用方经公共事务接口写入的 101 条 `unattributed` 记录。汇总保留这些记录和它们的时间。每项统计使用最近秩分位数，并列出每类耗时最大的五次尝试及其 Run。六条样本的 p95 等于最大值。原 Phase 2A 的 94 条历史样本重放得到相同汇总，其中 35 条历史写事务仅有原始计时字段。[统计程序](acceptance/phase2a/performance.py) · [历史统计](acceptance/phase2a/performance/summary.json)

## 回滚与复验

两次锁竞争各确认三次提交、两次回滚、一次 BEGIN 超时。嵌套回滚后提交仅计入一条持久化行。业务异常和延迟外键约束失败均计入零行，独立读取数据库可见三条预期消息，完整性和外键检查均通过。观测文件出口时，持有对应写事务的连接数为零。按原始 JSONL 独立重放，两份统计均与探针输出逐字节相同。测试环境使用 Python 3.11.9、SQLite 3.45.3。[真实锁夹具](acceptance/phase2a/sqlite_telemetry.py) · [归档内文件校验和](acceptance/closure/sqlite/4e2436e/raw-files.sha256)

统一入口会创建新的验收 Run 并归档原始证据。

```sh
bin/gx-acceptance run phase2a --case DB01 --case DB02
```

也可以独立运行夹具或统计已有的 GX 指标文件。第一条命令需要新的或空的输出目录。

```sh
python acceptance/phase2a/sqlite_telemetry.py --output /tmp/gx-sqlite-proof
python acceptance/phase2a/performance.py --transaction-metrics /tmp/gx-sqlite-proof/metrics.jsonl --output /tmp/gx-sqlite-summary
```
