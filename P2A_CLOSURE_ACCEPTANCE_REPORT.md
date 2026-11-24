# P2A Closure 集成验收

**签收结论为 PARTIAL，37 项通过，1 项部分完成，0 项失败。** 冻结版 `3a08a9d1c36759686d29743c75d789cd959848a1` 于 2026-11-24 16:04–16:11 UTC 在干净的独立工作树完成全量运行。原 Phase 2A 通过 15/15，Closure 通过 19/20，附加检查通过 3/3。T03 已逐项复跑原有 465 个测试，461 个通过、4 个沿用原跳过。另一次历史运行报告的 42 项失败缺少测试身份与原始错误，可独立归因 0/42。[冻结版逐项结果](acceptance/closure/runs/frozen-3a08a9d/summary.json) · [版本与环境](acceptance/closure/runs/frozen-3a08a9d/environment.json)

## 运行记录与修复

| 运行 | 实际执行范围 | 结果 |
|---|---|---|
| [full-d5df10e-20261124T1533Z](acceptance/closure/runs/full-d5df10e-20261124T1533Z/summary.json) | `d5df10e` · 全量 38 项 | FAIL（32 PASS, 6 FAIL） |
| [frozen-c28f9b0](acceptance/closure/runs/frozen-c28f9b0/summary.json) | `c28f9b0` · 全量 38 项 | PARTIAL（37 PASS, 1 PARTIAL） |
| [frozen-fa8bba9](acceptance/closure/runs/frozen-fa8bba9/summary.json) | `fa8bba9` · 全量 38 项 | FAIL（33 PASS, 4 FAIL, 1 PARTIAL） |
| [frozen-3a08a9d](acceptance/closure/runs/frozen-3a08a9d/summary.json) | `3a08a9d` · 全量 38 项 | PARTIAL（37 PASS, 1 PARTIAL） |
| [retry-3a08a9d](acceptance/closure/runs/retry-3a08a9d/summary.json) | `3a08a9d` · 仅复跑 T03 | PARTIAL（1 PARTIAL） |

首轮全量从干净的分支启动，回归操作使用具名提交，其他直接探针使用当时的工作树。首轮 474 个 Python 用例有 469 通过、1 个错误、4 个既有跳过。按原始 465 个身份看是 460 通过、1 个错误、4 个既有跳过。P2A-09 缺预期的取消通知副本。P2A-10、11、12、REG01 的共用套件和 T03 记录了同一次混合工作区恢复错误，两个 CPU Job 插槽当时都已被占用。首轮共保留 308 份原始文件。[首轮失败详情](acceptance/closure/runs/full-d5df10e-20261124T1533Z/summary.json) · [首轮原件](acceptance/closure/runs/full-d5df10e-20261124T1533Z/raw.tar.gz)

修复后的协议探针核对同一线程、Turn、call 和官方核心日志中的精确取消原因，错误原因的负对照仍失败。可重放的节点在临时 CPU 紧缺时持久化等待，容量释放后继续原工作。用量修复分别覆盖多资金来源、已关闭或缺明细的历史预留，以及只有新增原始回执的未预留 Turn。在 `c28f9b0` 的 37 项通过后，独立复核发现未预留 Turn 的原始回执边界。该边界在 `fa8bba9` 修复。[取消观测与负对照](acceptance/closure/terminal-cancellation/evidence.json) · [CPU 准入复现](acceptance/closure/idle-history/cpu-admission/receipt.json) · [用量反例与账本](P2A_09_USAGE_RECONCILIATION_REPORT.md)

在 `fa8bba9`，Python 发现沿用原 4 个跳过，其余 472 项通过，终态协议通过 204/204。官方迟到子线程探针的八项断言中，只有后续请求计数失败，使 P2A-10、11、12 和 REG01 四项沿用共用套件的失败。原 Turn 已在子线程到达后准确结算 253，后续 Turn 使总数达 288。强制延后子线程的独立试验实测四个阶段分别有 2、2、3、4 个 HTTP 请求。修正后的夹具在迟到子线程结算完成、后续 Turn 启动前取样，核对原有请求全保留且仅新增一次父线程请求。早期 AC02 的原失败和单项重跑也已分别保留。[四阶段请求和负对照](acceptance/closure/child-request-order/comparison.json) · [原 AC02 失败](acceptance/closure/automation-history/acdb-first/summary.json) · [AC02 修复后单项复跑](acceptance/closure/automation-history/acdb-retry/summary.json)

## Closure 的 20 项

| 项目 | 结果 | 验收内容 |
|---|---|---|
| U01 | PASS | 可归属 Turn 的官方响应按身份精确结算一次。 |
| U02 | PASS | 自然中断缺完成凭据，宽限期内保持 HELD。 |
| U03 | PASS | Driver 的维护循环在到期后追加不可变估算。 |
| U04 | PASS | 迟到凭据关联原估算，按差额调整。 |
| U05 | PASS | 重复、乱序和冲突回执保持幂等并保留未决事实。 |
| U06 | PASS | 父子线程独立归属，同一响应只计一次。 |
| U07 | PASS | 原始子线程累计 915，四笔响应合计 732，差额 183 无归属。 |
| U08 | PASS | GX 跨实际期限重启，维护只结算一次。 |
| T01 | PASS | 两核、两个 Job 线程、零 GPU 的确定性准入与释放。 |
| T02 | PASS | 公开 run_start 在带 CLAUDE 标记的父环境下正确识别子进程。 |
| T03 | PARTIAL | 旧 465 个测试身份的同版复核与原 42 个历史失败归因。 |
| DB01 | PASS | 真实 SQLite 锁竞争，慢样本含业务 owner、operation、run_id。 |
| DB02 | PASS | 分别测量获取写锁、事务体及提交或回滚。 |
| AC01 | PASS | 一条命令留下版本、环境、命令、退出码与日志。 |
| AC02 | PASS | 解包并逐成员验 SHA-256，完整性负对照被识别。 |
| AC03 | PASS | 仅失败项重跑，已完成的历史结果和原包均可复查。 |
| IDLE01 | PASS | Driver 连续空闲时逐步退避，达到上限后保持。 |
| IDLE02 | PASS | 新事件、Job 消息和系统信号打断等待。 |
| IDLE03 | PASS | 下一次等待遵守最近 Job、Usage 和 Lease 期限。 |
| REG01 | PASS | 同冻结版执行原回归，核对保留的原始测试身份。 |

最终 Python 发现共 476 项，472 通过、4 个原有可选 DeepSeek SDK 用例跳过，0 个错误、0 个断言失败。其中原 465 项有 461 通过、4 个原有跳过，新增 11 项均通过。另有 127 项 P0 专项、2 个 Node 测试、13 个专用原生场景和 17 个既有官方宿主场景通过。最终单项续跑只执行 T03，复用校验过的父包发现日志，耗时 0.697 秒，退出码为 2。续跑回执记录的父清单哈希与冻结全量包一致。原 42 项的身份、错误与分类证据仍缺失。[测试环境与历史缺口](P2A_TEST_ENVIRONMENT_REPORT.md) · [单项续跑命令与退出码](acceptance/closure/runs/retry-3a08a9d/commands.json) · [逐项证据目录](acceptance/closure/runs/frozen-3a08a9d/raw-files.sha256)

P2A-14 的两次离线工作负载共记录 36 个最外层写事务，总耗时 p50、p95、最大值依次为 0.814、50.672、99.701 ms。DB01 和 DB02 的双连接用例包含 6 次写事务尝试，测得最长写锁等待 178.740 ms，另一个连接的最长事务体为 150.355 ms。[事务指标口径](P2A_SQLITE_TRANSACTION_TELEMETRY_REPORT.md) · [空闲唤醒](P2A_IDLE_BACKOFF_REPORT.md) · [验收归档](P2A_ACCEPTANCE_AUTOMATION_REPORT.md)

## 原 Phase 2A 的 15 项

| 项目 | 结果 | 验收内容 |
|---|---|---|
| P2A-01 | PASS | A1 等待决策时，B2 汇总和 B3 数值检查完成。 |
| P2A-02 | PASS | C1 记录其执行时 A1 与 B1 的等待状态。 |
| P2A-03 | PASS | 停止官方 Codex 进程后 GX 继续完成独立节点。 |
| P2A-04 | PASS | 终止 GX 并打开原数据库，恢复原 Job 及其产物。 |
| P2A-05 | PASS | 待对账的原生预留与本地计算并存。 |
| P2A-06 | PASS | 冷恢复后已完成节点、产物和结算均无重复。 |
| P2A-07 | PASS | 保留原等待引用及其持有者，依赖节点未提前执行。 |
| P2A-08 | PASS | 独立分支失败回滚，重试后才执行后继节点。 |
| P2A-09 | PASS | 终态提交、取消、原 Turn 恢复和可归属/缺失用量处理。 |
| P2A-10 | PASS | 错误 schema 后在原 Attempt 修复，32 路 schema 分页重组。 |
| P2A-11 | PASS | 核对原生 child 工具、作用域及丢失启动通知恢复。 |
| P2A-12 | PASS | 重放保留的 Brief 输入并以真实宿主检查线路与资料更新。 |
| P2A-13 | PASS | 公开 CLI、旧表内容保留、冷 checkpoint 和终态恢复。 |
| P2A-14 | PASS | 实测停止宿主后的空闲 Driver、Job 对账及 SQLite 延迟。 |
| P2A-15 | PASS | 后端暂不可读的 Job 延后对账，另一 Job 可在同轮结算。 |

停用官方宿主后，持续运行和冷重启两组都在本地完成 B2、B3，重启组还完成 C1。观测到 0 次额外宿主启动，重复节点、Artifact 和结算分别为 0、0、0。另有一项矩阵外的子线程观测同时丢失热连接的 spawn 通知和冷恢复的 start 通知，结果为 `COLD_SETTLEMENT_TIMEOUT`、内层退出码 1，在 25 秒的内层窗口中未完成冷结算。这一补充结果保留在 `v31-regression/evidence/extra-child/observation.json`。[跨进程、Job 与历史回归原件](acceptance/closure/runs/frozen-3a08a9d/raw.tar.gz) · [原 Phase 2A 结论](PHASE_2A_ACCEPTANCE_REPORT.md)

## 附加检查与证据等级

| 项目 | 结果 | 验收内容 |
|---|---|---|
| OFFICIAL-USAGE | PASS | 锁定的官方宿主，脚本化上游，正常父子响应和自然中断。 |
| REAL_RESOURCES | PASS | 在真实机器资源配置下准入、运行并收集本地 CPU Job 产物。 |
| LEGACY-LEDGER | PASS | 可写升级前后核对两份旧库的历史数据，并重复打开。 |

**官方宿主环回。** 锁定的 Codex CLI 0.169.0 连接本地脚本上游。P2A-09 的 204 回合有 103 次终态接受、101 次终态拒绝。204 笔用量记录均为 `PROVIDER_CONFIRMED`，终态后的新增模型请求数为 0。

最终冻结版与前一次 `fa8bba9` 快照的 `gx-h/` 产品树均为 `996f926de8cb5c5d8fa900749abf64ea2c31fdc4`。`fa8bba9` 的 204 回合有 203 笔经提供者确认、1 笔估算。自然发生的 `GX_ABORT_0187` 未注入回执故障，已确认响应下界为 35。宽限期内原预留保持 HELD，30 秒到期后 Driver 追加 800 的预算估算，超过期限约 5 ms 更新，重启仍为一次结算。另一官方专项分别归属父 70、子 183，合计 253，仅结算一次。其独立中断案例观察到 35 的响应下界，30 秒宽限期后按预算估算 1000，重启后仍仅结算一次。[最终官方宿主逐回合原件](acceptance/closure/runs/frozen-3a08a9d/raw.tar.gz) · [自然中断原件](acceptance/closure/runs/frozen-fa8bba9/raw.tar.gz)

**脚本化回放。** U07 复用历史官方记录，子线程四笔回执合计 732、累计 915，保留 183 为 `UNATTRIBUTED_DELTA`。父线程自身回执合计 105，两条线程合计可归属 837。冻结的 Usage 记录共 21 项通过，包括 U01–U08、无可用额度对照和 12 个资金归属边界。缺乏可用估算依据的原预留保持 `UNRESOLVED`。独立用量边界复核有 13 个可执行反例。四轮对照依次通过 4/13、10/13、12/13、13/13，保留每次判定及原始数据。[计量定义与专项证据](P2A_09_USAGE_RECONCILIATION_REPORT.md) · [独立反例回执](acceptance/closure/usage-history/funding-fa8bba9/receipt.json)

**真实本机计算。** REAL_RESOURCES 从 cgroup 读得 24 核配额，按配置准入最多 8 个线程，并读取了本机设备。CPU Job 状态为 `SUCCEEDED`，产生 3 个输出。历史兼容检查分别验证两份旧库的 118、118 张原表，并重复可写打开，原有 1 笔 HELD 预留与 3 笔已结算记录保留。本次验收归档的执行等级是 `SCRIPTED`、`OFFICIAL_HOST_LOOPBACK` 和 `REAL_COMPUTE`。新增 `LIVE_MODEL` 与 `INDEPENDENT_REVIEW` 类别验收条目均为零。[执行环境与证据分类](acceptance/closure/runs/frozen-3a08a9d/commands.json)

## 复查与恢复

冻结版归档含 345 个原始文件，包括 50 份通过 SQLite quick_check 的快照和 37 份事件文件。压缩包 SHA-256 为 `2135c0d90b3714e6bae1810174d2555de6ecaf7f41fd037f2face507cba82902`，外层清单 SHA-256 为 `3145ef0df2bfa92e7bfa087057e39d145671e5bd775dbcd31449f4224bc873df`。外层清单校验元数据和压缩包，内层清单逐个校验原始文件。[调用和退出码](acceptance/closure/runs/frozen-3a08a9d/commands.json) · [SHA-256 清单](acceptance/closure/runs/frozen-3a08a9d/manifest.sha256) · [原始包](acceptance/closure/runs/frozen-3a08a9d/raw.tar.gz) · [历次运行索引](acceptance/closure/runs/index.json)

在发行目录运行以下命令可读取冻结结果，并将校验通过的原件解包到新目录。[完整命令和环境参数](acceptance/closure/runs/frozen-3a08a9d/environment.json)

```sh
bin/gx-acceptance report frozen-3a08a9d --runs-dir acceptance/closure/runs
bin/gx-acceptance verify frozen-3a08a9d --runs-dir acceptance/closure/runs --extract-to /tmp/gx-p2a-3a08a9d-evidence
```

重新执行全量使用 `bin/gx-acceptance run all`。下列命令仅复跑未签收项，并保留原包。取得原 42 项失败的 JUnit 后，在这条命令末尾加上 `--original-failures /path/to/original-junit.xml` 即可重新归因。[本次失败项续跑与来源](acceptance/closure/runs/retry-3a08a9d/summary.json)

```sh
bin/gx-acceptance run all --failed-only frozen-3a08a9d --runs-dir acceptance/closure/runs
```

升级或恢复时先停止原 Driver，保存 SQLite、Artifact、Job 侧车与预留，随后在副本上用 `bin/gx-p0 status --state DIR --run-id RUN` 核对。回退使用升级前完整备份及匹配版本，接管已有 Job 前核对原外部结果。实际测试范围、所需历史输入与旧版回退演练见运行说明。[限制与恢复步骤](P2A_CLOSURE_LIMITATIONS.md) · [历史回退演练](PHASE_2A_ROLLBACK_REPORT.md) · [实施前调用链审计](P2A_CLOSURE_SOURCE_AUDIT.md)
