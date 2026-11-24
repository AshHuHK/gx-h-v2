# P2A.1 边界修复与签收

本轮以 `61ea96d` 为起点，沿用 Codex 的原生线程和 GX 的 Driver、Job、预算及 SQLite 状态。P2A 的签收条件同时包括本轮实测的不变量，以及 T03 要求的原始 42 项失败归因。[原版冻结记录](P2A_CLOSURE_ACCEPTANCE_REPORT.md)

## 怎样复跑

在仓库根目录执行。将锁定 Python 和 Node 安装的 bin 目录置于 PATH 前部，或用该 Python 显式执行 `bin/gx-acceptance` 并通过 `--python`、`--node` 指定执行文件。验收使用 CPython 3.11.9、Node 22.17.0 和 npm 10.9.2。Python wheels、Cordis 包和锁文件随仓库交付。运行冻结用例还需要与 `release-assets/lock.json` 一致的 Codex 可执行文件，以及原有历史夹具。可用 `bin/gx-acceptance run --help` 查看如何指定本机缓存、历史数据库、Clash 和运行输出目录。[发行锁](release-assets/lock.json)

```bash
bin/gx-acceptance run all --run-id p2a1-baseline
bin/gx-acceptance run hardening --run-id p2a1-hardening --discovery-run p2a1-baseline
bin/gx-acceptance report p2a1-hardening
bin/gx-acceptance verify p2a1-hardening
```

`--discovery-run` 复用相同源码、环境与依赖校验通过的原始 Python 发现日志。运行器仍独立核对各测试身份和原来的跳过理由。PARTIAL 的退出码为 2，逐项状态在 `summary.json`。新实验使用新的 Run ID，以保留旧版回执。[验收入口](acceptance/closure/runner.py)

## T03 原始材料

用户随后交付了原修订 `78a8b07` 的分类 JSON 和原始运行日志。日志记录 465 项、34 个错误、8 个失败和 4 项跳过。42 个失败标题对应 40 个方法，两个方法各有两个不同参数的 subTest。已逐项核对异常原文、参数和失败类型，并保存原 traceback 摘要。分类文件记录的环境为 Python 3.13.18、SQLite 3.53.1、Node 22.23.5，宿主有 2 个逻辑 CPU。[原分类与环境](gx-historical-42-classification.json) · [原始运行日志](gx-historical-42-failures-78a8b07.log)

其中，38 项原栈直接记录单线程容量拒绝，2 项原栈直接记录 Node 找不到 Cordis。另一项只报告 Job 数量 2 与期望 3 不符，还有一项报告 reviewer 为 `unavailable`。在原修订的独立对照中，模拟的 cgroup 配额从 2 核变为 4 核令 Job 准入从 1 线程变为 2 线程，原来的 2 个 Job 变为预期的 3 个。另一个对照只安装锁定的 Biopython 1.88，reviewer 从 `unavailable` 返回预期的 `disagrees`；未安装时复现了相同断言，并捕获缺少 Bio 的异常。原 reviewer 断言本身只显示了状态差异。[同源码对照及原始输出](acceptance/hardening/historical-controls/README.md)分类文件也提及独立的 476 项复跑，该轮报告了 13 项失败或错误，原日志未随本次两份文件交付。前期检查的 23 份归档及当时的信息缺口保留在原记录中。[收到历史原件前的查找记录](acceptance/closure/profile-history/p2a1-source-search.json)

发行目录中随附这次使用的分类文件和原始日志。复核其他目录中的材料，可把分类 JSON、它引用的逐项证据和环境记录放在一起，再使用下面的入口。JSON 的 `failures` 数组按原始失败项登记 `test_id`、`classification`、`reason` 和 `evidence`。同一方法的多个失败可用 `original_id` 区分。每个证据可携带 `evidence_sha256`，顶层也可附 `original_run_log`、`environment_file` 及对应的 `_sha256`。验收会归档读到的字节，核对所附运行日志和分类的用例身份，保留无法归因的项。[录入实现](acceptance/closure/profile_cases.py) · [录入正反对照](acceptance/hardening/test_historical_intake.py)

```bash
bin/gx-acceptance run hardening --case T03 --discovery-run p2a1-baseline \
  --original-failures /path/to/original/classification.json --run-id p2a1-original-42
```

## 实测不变量

| 范围 | 观察与判据 |
| --- | --- |
| Child 冷恢复 | 实际 Codex 0.169.0 启动一个子线程。GX 丢失 spawn 通知并被 SIGKILL；新的 GX 进程启动后，接力层丢弃此前截留的同一 child 启动帧。恢复时核对父端 spawn 历史、子端原 Turn、终态及时间。5 秒宽限到期后，Driver 对原预留产生一条估算结算。再次启动后仍只有同一结算、同一 CPU Job 和三次原模型请求。旧修订也可能做估算，但同一实验中其子线程仍未核验，缺少子 Turn 用量行。[两次恢复与原生帧](acceptance/p2a1/child-double-loss) |
| 用量估算 | 预留 800、可归属下界 1,000 时按 1,000 临时入账，未观察部分的上限显示未知。完整 1,800 回执到达后只追加 800 并保留原估算。预留大于下界的另一条路径也已复跑。[两种输入与账本](P2A1_ESTIMATE_RISK_REPORT.md) |
| 共享 FIFO | 前任退出后，持有原 inode 的后任能收到新的 Job 字节。另一读取者已取走提示、旧 FD 指向已断开的 inode 或提示通道不可用时，等待器仍以 SQLite 提交或原生 FD 唤醒。正常空闲时 data_version 检查约每秒 4 次。[交接与等待成本](P2A_IDLE_BACKOFF_REPORT.md) |
| 测试配置与依赖 | 生产 CLI 和服务入口检测误设的测试环境。受控验收通过仍存活的祖先进程持有的内核文件锁选择固定容量，单独提供两个环境变量无法启用固定模式。每次新 Run 保存实际采用的 CPU、内存和 GPU 配置。依赖在全新私有环境离线安装，校验版本与包哈希。[依赖、超时和文件句柄证据](acceptance/hardening/dependency-timeout/evidence.json) |
| CPU 准入 | 已在工作流中排队的 2 线程 Job 保留队首。仅释放一个线程时，两个较晚的 1 线程请求等待；释放第二个线程并冷重开后，队首先获得资源。直接提交也遵守同一准入决策。排队状态公开需求、等待时长与可准入性，暂停、取消、撤销或不可能满足的请求按其工作流状态处理。[跨进程真实 Job 实验](acceptance/hardening/cpu_fairness.py) |
| 超时与连接释放 | 真实子进程在 1 秒墙钟期限被 Runner 终止。另在 Python 3.13 实测五处测试连接从产生五项未关闭告警降为零。旧日志中的四条警告缺乏连接创建栈，因此保留其来源的不确定性。[诊断与原始输出](acceptance/hardening/dependency-timeout/evidence.json) |

## 操作员处理未决预留

先查看 Run 内的 `metering.unresolved_cases` 和 `metering.unresolved_alerts`。默认提醒分别在 1、24、72 小时到达，在 Run 的 `p0_spec.metering.unresolved_alert_seconds` 中可指定三个递增秒数。离线维护和状态读取沿用同一组阈值。动作按 Run、预留和消息 ID 记录操作人、原因、前后状态与官方证据引用。[操作员入口](gx-h/gx_core/p0/operator.py) · [未决用量处理](gx-h/gx_core/p0/operator_usage.py)

```bash
bin/gx-p0 operator usage --state DIR --run-id RID
bin/gx-p0 operator usage-resolve --state DIR --run-id RID --reservation-ref REF \
  --decision attach-evidence --source codex-rollout --thread-id THREAD --turn-id TURN \
  --response-id RESPONSE --message-id ATTACH_ID --reason '已取得该次响应的原生使用记录'
bin/gx-p0 operator usage-resolve --state DIR --run-id RID --reservation-ref REF \
  --decision reconcile --message-id RECONCILE_ID --reason '使用已核验的回执重新对账'
```

在证据仍不足时，`keep-unresolved` 可以保留冻结并记录原因。确需承担风险时，先以 `accept-exposure` 记录决定，再在 `release-hold` 中传入返回的 `operator_event_id` 作为 `--acceptance-event-id`，同时显式传入 `--approve-release`。同一命令重试使用原 `--message-id`；新的决定使用新的 ID。释放保留原预留记录，后续完整回执按原资金账户登记可核对的差额。[预算处理](gx-h/gx_core/budgets/__init__.py)

## 升级和回退

停止原 Driver，保留完整工作区的备份，包括 SQLite、Artifact 和已有 Job 的侧车文件。先在副本上用本版 `bin/gx-p0 status --state DIR --run-id RID` 核对已结金额、未决预留、Job 和产物，再开启可写运行。历史 Run 保留其原有启动信息；新 Run 保存启动时的 CPU、内存和 GPU 配置。回退时停止新版，恢复升级前的完整备份，使用对应旧版本读取，再核对仍在途的外部 Job。[历史迁移和回退实现](acceptance/closure/legacy_ledger.py)

实测包含两个有 118 张原表的历史项目，以及 U05、B05 两份各有 120 张原表的旧库。后两份库连续两次升级打开时，未决时间取自原记录，人工释放记录数均为零。恢复原备份后，以 `3a08a9d` 的 CLI 分别读到 130 和 120 token 已结、各 100 token 在途；原事件和预算表摘要匹配。[独立历史恢复回执](acceptance/p2a1/legacy-ledger-f63d1a8-local/summary.json)

## P2B 开发门槛

GraphVersion 的方案构建、影响范围计算与结果复用判断应在 SQLite 写事务外完成。提交时在短事务内核对同一 Run 的当前 head、预期基线版本及摘要，原子写入新版本与事件。CAS 冲突后读取新 head，重新计算受影响范围。P2B 验收需给出并发改图、崩溃恢复和相应写锁时间线。[已有单头版本决策](V32_PHASE_1_5_DELTA.md) · [Amendment ADR](p2/ADRs/ADR-04.md)
