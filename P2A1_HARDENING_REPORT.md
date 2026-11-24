# P2A.1 边界修复与签收

**P2A 已关闭，本地冻结标签为 `v3.2-p2a.1`。** 同一提交通过公开全量 38/38、P2A.1 专项 13/13。T03 根据原始日志、分类、对照和复跑正式关闭。该标签包括下列验收与交接材料。[全量结果](acceptance/hardening/runs/p2a1-baseline-5454500/summary.json) · [专项结果](acceptance/hardening/runs/p2a1-hardening-5454500/summary.json)

本轮以 `61ea96d` 为起点，沿用 Codex 原生线程与 GX 的 Driver、Job、预算及 SQLite 状态。原 Closure 交付的结论和每次失败仍可按提交核对。[原版冻结记录](P2A_CLOSURE_ACCEPTANCE_REPORT.md) · [历次验收索引](acceptance/hardening/runs/index.json)

2026-11-24，干净检出 `5454500e3c30751221cacd71000a17e44289dba2` 从 18:24:14 至 18:33:07 UTC 完成公开全量验收，原 P2A 15 项、Closure 20 项及附加 3 项全部通过。REG01 引用本次同一提交上的全部 15 项。Python 的 482 项中 478 项通过、4 项沿用可选 DeepSeek SDK 的跳过条件。另外 128 项 P0 专项和 2 项 Node 测试通过，204 次协议实验提交之后没有追加模型请求。[全量 38 项回执](acceptance/hardening/runs/p2a1-baseline-5454500/summary.json) · [环境与实际命令](acceptance/hardening/runs/p2a1-baseline-5454500/environment.json)

随后的 13 项专项在 18:34:24 UTC 全部完成。两包分别含 365、159 份原始文件，51、16 份 SQLite 数据库通过 `quick_check`。发行标签与受测提交的 GX 产品树相同，树 ID 为 `453802b0d59cce30ef7095c39711539162fe0768`。后续唯一的验收执行代码变更为 `b2f8b43` 的 Child 测试临时目录修复，已在宿主 UID 0 和 65534 下分别复验直接与继承入口。[最终验收包与精确版本](acceptance/hardening/runs/index.json) · [四项补测与首次失败](acceptance/hardening/namespace-scope/evidence.json)

复核随标签交付的原始证据，可直接使用仓库内的归档目录。

```bash
bin/gx-acceptance verify p2a1-baseline-5454500 --runs-dir acceptance/hardening/runs
bin/gx-acceptance verify p2a1-hardening-5454500 --runs-dir acceptance/hardening/runs
```

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

其中，38 项原栈直接记录单线程容量拒绝，2 项原栈直接记录 Node 找不到 Cordis。另一项只报告 Job 数量 2 与期望 3 不符，还有一项报告 reviewer 为 `unavailable`。在原修订的独立对照中，模拟的 cgroup 配额从 2 核变为 4 核令 Job 准入从 1 线程变为 2 线程，原来的 2 个 Job 变为预期的 3 个。另一个对照只安装锁定的 Biopython 1.88，reviewer 从 `unavailable` 返回预期的 `disagrees`。未安装时复现了相同断言，并捕获缺少 Bio 的异常。原 reviewer 断言本身只显示了状态差异。[同源码对照及原始输出](acceptance/hardening/historical-controls/README.md)

分类文件也提及独立的 476 项复跑，该轮报告了 13 项失败或错误，原日志未随本次两份文件交付。前期检查的 23 份归档及当时的信息缺口保留在原记录中。[收到历史原件前的查找记录](acceptance/closure/profile-history/p2a1-source-search.json)

发行目录中随附这次使用的分类文件和原始日志。复核其他目录中的材料，可把分类 JSON、它引用的逐项证据和环境记录放在一起，再使用下面的入口。

JSON 的 `failures` 数组按原始失败项登记 `test_id`、`classification`、`reason` 和 `evidence`。同一方法的多个失败可用 `original_id` 区分。每个证据可携带 `evidence_sha256`，顶层也可附 `original_run_log`、`environment_file` 及对应的 `_sha256`。验收会归档读到的字节，核对所附运行日志和分类的用例身份，保留无法归因的项。[录入实现](acceptance/closure/profile_cases.py) · [录入正反对照](acceptance/hardening/test_historical_intake.py)

```bash
bin/gx-acceptance run hardening --case T03 --discovery-run p2a1-baseline \
  --original-failures /path/to/original/classification.json --run-id p2a1-original-42
```

## 实测不变量

| 范围 | 观察与判据 |
| --- | --- |
| Child 冷恢复 | 实际 Codex 0.169.0 启动一个子线程。GX 丢失 spawn 通知并被 SIGKILL。新的 GX 进程启动后，接力层丢弃此前截留的同一 child 启动帧。恢复时核对父端 spawn 历史、子端原 Turn、终态及时间。5 秒宽限到期后，Driver 对原预留产生一条估算结算。再次启动后仍只有同一结算、同一 CPU Job 和三次原模型请求。旧修订也可能做估算，但同一实验中其子线程仍未核验，缺少子 Turn 用量行。[双版本恢复、原生帧与结算](acceptance/p2a1/child-double-loss/README.md) |
| 用量估算 | 预留 800、可归属下界 1,000 时按 1,000 临时入账，未观察部分的上限显示未知。完整 1,800 回执到达后只追加 800 并保留原估算。预留大于下界的另一条路径也已复跑。[两种输入与账本](P2A1_ESTIMATE_RISK_REPORT.md) |
| 共享 FIFO | 前任退出后，持有原 inode 的后任能收到新的 Job 字节。另一读取者已取走提示、旧 FD 指向已断开的 inode 或提示通道不可用时，等待器仍以 SQLite 提交或原生 FD 唤醒。正常空闲时 data_version 检查约每秒 4 次。[交接与等待成本](P2A_IDLE_BACKOFF_REPORT.md) |
| 测试配置与依赖 | 生产 CLI 和服务入口检测误设的测试环境。受控验收通过仍存活的祖先进程持有的内核文件锁选择固定容量，单独提供两个环境变量无法启用固定模式。每次新 Run 保存实际采用的 CPU、内存和 GPU 配置。依赖在全新私有环境离线安装，校验版本与包哈希。[资源作用域回执](acceptance/p2a1/profile-history/profile-scope-28ce665/summary.json) · [离线依赖证据](acceptance/hardening/dependency-timeout/evidence.json) |
| CPU 准入 | 已在工作流中排队的 2 线程 Job 保留队首。仅释放一个线程时，两个较晚的 1 线程请求等待。释放第二个线程并冷重开后，队首先获得资源。直接提交也遵守同一准入决策。排队状态公开需求、等待时长与可准入性，暂停、取消、撤销或不可能满足的请求按其工作流状态处理。[五个原始 CPU Job 的回执](acceptance/hardening/fairness-98891bb/receipt.json) |
| SQLite | 审计本轮变更的生产事务边界，按 owner、operation、Run ID 记录锁等待、事务体和收尾。文件与解释器哈希、CPU/运行器和 GPU 探测以及历史回执扫描均在本轮测到的写事务之外。用增加到 400 个 Run 的夹具确认队首查找不遍历检查点。[写事务实测与已知边界](P2A1_SQLITE_BOUNDARIES.md) |
| 超时与连接释放 | 真实子进程在 1 秒墙钟期限被 Runner 终止。另在 Python 3.13 实测五处测试连接从产生五项未关闭告警降为零。旧日志中的四条警告缺乏连接创建栈，因此保留其来源的不确定性。[诊断与原始输出](acceptance/hardening/dependency-timeout/evidence.json) |

在完整套件的两个模型恢复用例中，子进程曾在发送 HTTP 之前误报测试资源锁失效。以无关进程增删 POSIX 锁，旧读法在 30,740 次查询中漏读 1,036 次，同时能够从持有者的文件描述符证实锁仍在。GX 现在读取该描述符的内核锁信息并核对持有者、祖先关系及锁范围。同样负载下，原两个用例重复 60 次的结果从 4 次失败变为全数通过。无锁、错误范围、非祖先和已退出的持有者仍被拒绝。[原始复现、锁记录和修复后对照](acceptance/hardening/model-activity-scope/evidence.json)

Linux 用户命名空间还可能禁止读取外层持锁进程的文件描述符。受控验收入口先在外层核验锁，再由内层启动器为其后代持有本地锁。实测外层 UID 0 和 65534 映射到内层 UID 0，两者都能保留正确的资源配置。在 UID 65534 的额外试验中，Child 夹具首次启动未继承私有临时目录。修订 `b2f8b43` 将同一实验的两个 GX 工作者置于其私有目录，两个 UID 的公开入口与继承入口共 4/4 通过，每次都只产生一次原预留估算，恢复后新增模型请求为零。[原始失败、两个 UID 与四次复验](acceptance/hardening/namespace-scope/evidence.json)

## 工具取消的官方证据

在 `3940eb5` 的第 60 次实验，GX 已拒绝提交，官方历史记录了同一工具调用失败。两条实时回执均缺失，当时的探针又在保存官方持久回执前退出。这一次的精确取消原因保留为 **UNPROVEN**。[第 60 次的原始记录与证据边界](acceptance/p2a1/terminal-cancellation/1f8fc82/original-3940eb5-assessment.json)

在干净版本 `1f8fc82`，探针能用官方持久记录核对 thread、turn、call、工具名及精确的取消原因，再检查公开历史、空工具结果、普通 Tool Response 数量和后续模型请求。独立执行的 204 次中，103 项按预期接受、101 项按预期拒绝，提交后产生 0 次模型请求。一次自然发生的双通知缺失和一次主动丢弃双通知都取得了精确取消证据。另用同一条真实回执构造了 11 个反例，丢失持久记录、错误作用域、原因不符、回执重复等情形均被拒绝。[204 次原始输出](acceptance/p2a1/terminal-cancellation/1f8fc82/full.json.gz) · [主动丢帧](acceptance/p2a1/terminal-cancellation/1f8fc82/lost-mirrors.json) · [反例结果](acceptance/p2a1/terminal-cancellation/1f8fc82/negative-controls.json)

在公共入口的另一次实验，第一次读取原 Turn 时，工具已失败，Turn 仍为 `inProgress`。丢帧夹具随后等到了自己的超时。修正后的夹具通过同一 Turn 的官方历史等待 `interrupted`。四项控制中每项都只有一次中断到达宿主、一次 35-token 结算。一直不转交中断的负例在 8 秒内观察 51 次 `inProgress` 后准确失败。[原始失败、同源码对照与有界恢复](acceptance/p2a1/terminal-receipt-loss/3e52d11/README.md)

## 操作员处理未决预留

正式宿主专项扣留了一份 35 token 的官方响应回执。GX 进入 UNRESOLVED，操作员附上由 Codex 返回路径中的官方记录后，预留继续保持 HELD。独立的 reconcile 操作确认 35 token 和一次调用，重放后余额相同。一笔 38 token 的旧已付款记录也能在缺少 Turn 归属时进入待办。取得确切官方回执后的对账差额为零。[七项操作员场景和官方宿主回执](acceptance/hardening/usage-resolution/review.json)

先查看 Run 内的 `metering.unresolved_cases` 和 `metering.unresolved_alerts`。默认提醒分别在 1、24、72 小时到达，在 Run 的 `p0_spec.metering.unresolved_alert_seconds` 中可指定三个递增秒数。离线维护和状态读取沿用同一组阈值。动作按 Run、预留和消息 ID 记录操作人、原因、前后状态与官方证据引用。[操作员入口](gx-h/gx_core/p0/operator.py) · [未决用量处理](gx-h/gx_core/p0/operator_usage.py)

```bash
bin/gx-p0 operator usage --state DIR --run-id RID
bin/gx-p0 operator usage-resolve --state DIR --run-id RID --reservation-ref REF \
  --decision attach-evidence --source codex-rollout --thread-id THREAD --turn-id TURN \
  --response-id RESPONSE --message-id ATTACH_ID --reason '已取得该次响应的原生使用记录'
bin/gx-p0 operator usage-resolve --state DIR --run-id RID --reservation-ref REF \
  --decision reconcile --message-id RECONCILE_ID --reason '使用已核验的回执重新对账'
```

在证据仍不足时，`keep-unresolved` 可以保留冻结并记录原因。确需承担风险时，先以 `accept-exposure` 记录决定，再在 `release-hold` 中传入返回的 `operator_event_id` 作为 `--acceptance-event-id`，同时显式传入 `--approve-release`。同一命令重试使用原 `--message-id`。新的决定使用新的 ID。释放保留原预留记录，后续完整回执按原资金账户登记可核对的差额。[预算处理](gx-h/gx_core/budgets/__init__.py)

## 简报对照与迟到计量

公共套件曾捕获一次 180,000-token 估算。该 Turn 实际有三条各 35-token 的响应，最后一条公开累计通知停在 70。保存的官方记录中，第三条响应给出自己的 35 和本 Turn 累计 105，随后是同线程、同 Turn、同工具调用的精确取消和终态。开启现有的可选宿主记录读取后，GX 能据此在原 180,000 估算之后追加一次 −179,895 的调整。另一个 Turn 的 105 保持不变，合计 210。仅提供公开通知、取消作用域错误或后续用量仍不完整时，保留原估算。[原账本、原始宿主记录和反例](acceptance/p2a1/brief-terminal/7a6f521/README.md)

简报实验的两端都显式启用相同的宿主计量证据源，并将脚本服务响应与 GX 账本分别统计。审查用例从 B2 的 13 次请求、455 token 降至 B3 的 6 次、210 token。确定性用例从 6 次、210 token 降至 3 次、105 token。两端的计算结果相同，终态之后无新模型请求。这里的 token 仅描述固定本地响应的对照。[A/B 原始数据](acceptance/p2a1/brief-terminal/7a6f521/brief-wire.json)

## 升级和回退

停止原 Driver，保留完整工作区的备份，包括 SQLite、Artifact 和已有 Job 的侧车文件。先在副本上用本版 `bin/gx-p0 status --state DIR --run-id RID` 核对已结金额、未决预留、Job 和产物，再开启可写运行。历史 Run 保留其原有启动信息。新 Run 保存启动时的 CPU、内存和 GPU 配置。回退时停止新版，恢复升级前的完整备份，使用对应旧版本读取，再核对仍在途的外部 Job。[历史迁移和回退实现](acceptance/closure/legacy_ledger.py)

实测包含两个有 118 张原表的历史项目，以及 U05、B05 两份各有 120 张原表的旧库。后两份库连续两次升级打开时，未决时间取自原记录，人工释放记录数均为零。恢复原备份后，以 `3a08a9d` 的 CLI 分别读到 130 和 120 token 已结、各 100 token 在途。原事件和预算表摘要匹配。[独立历史恢复回执](acceptance/p2a1/legacy-ledger-f63d1a8-local/summary.json)

## 验证范围与已知边界

宿主试验使用哈希锁定的官方 Codex 0.169.0，模型响应来自隔离环回网络中的脚本服务。Job、GX 进程重启、SQLite 和本机 CPU 均实际运行。本轮没有产生新的付费模型或外部集群样本。工作区通知的延迟受数据库写锁与机器调度影响。CPU 顺序保证适用于可运行且容量可满足的等待节点，以正在占用的资源最终释放为前提。[宿主和恢复证据](acceptance/p2a1/child-double-loss/README.md) · [CPU 队列实测](acceptance/hardening/fairness-98891bb/receipt.json)

原有可选输入 Artifact 的复制仍位于 `Run.run_start` 写事务内，1 MiB 样本实测 6.175 ms。本批新增事务的标签与重操作检查限定于专项列出的业务路径。[SQLite 实测边界](P2A1_SQLITE_BOUNDARIES.md)

## P2B 开发门槛

GraphVersion 的方案构建、影响范围计算与结果复用判断应在 SQLite 写事务外完成。提交时在短事务内核对同一 Run 的当前 head、预期基线版本及摘要，原子写入新版本与事件。CAS 冲突后读取新 head，重新计算受影响范围。P2B 验收需给出并发改图、崩溃恢复和相应写锁时间线。[已有单头版本决策](V32_PHASE_1_5_DELTA.md) · [Amendment ADR](p2/ADRs/ADR-04.md)
