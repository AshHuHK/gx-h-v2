# Phase 2A 验收命令和证据

从发布仓库执行以下命令可在当前版本重跑验收。所有输入取自本地。输出默认保存在 `var/acceptance/<run-id>/`。程序先打印本次运行 ID 和绝对目录，结束时打印结论、归档成员数和压缩包 SHA-256。

```sh
bin/gx-acceptance run all
bin/gx-acceptance report <run-id>
bin/gx-acceptance verify <run-id> --extract-to /tmp/gx-evidence-new
bin/gx-acceptance run all --failed-only <run-id>
```

`run phase2a` 执行原有 15 个矩阵项、Closure 的 20 个矩阵项，以及官方宿主用量专项。`run all` 另外执行独立的真实机器资源探针和历史账本兼容探针。`--case` 可重复使用，例如 `bin/gx-acceptance run phase2a --case P2A-08 --case U07`。所有子命令接受 `--runs-dir`。向每个子命令传入相同的目录即可读取迁移后的整组归档。源代码入口和矩阵定义可分别查看。[统一命令](bin/gx-acceptance) · [探针与矩阵项映射](acceptance/closure/cases.py)

## 输入及测试分类

默认从 `../gx-h-v-3` 读取缓存的官方 Codex 0.169.0、离线依赖及冻结的旧数据库，从 `../scratchpad/v3-real-performance/p1-2476f2b-xhigh` 读取历史 Brief 和模型目录。仓库自带运行时缓存时优先用本仓库。其他位置可通过 `--cache`、`--source-db`、`--historical-briefs`、`--catalog`、`--python`、`--binary`、`--clash` 指定。核对旧 42 个失败的原始日志可通过 `--original-failures` 提供。每次都记录实际输入路径。[测试环境构造](acceptance/closure/profile.py) · [原回归依赖校验](acceptance/phase2a/regression/run.py)

官方协议用例运行于仅启用环回接口的独立网络命名空间，Mihomo 监听 `127.0.0.1:7890/7891` 并拒绝外连。命令会保存官方二进制版本、哈希、命名空间及监听回执。合成输入和本地响应记为 `SCRIPTED`，实际启动发布锁指定的官方宿主另记 `OFFICIAL_HOST_LOOPBACK`。只有单独运行、等待并核对本机 CPU Job 产物的真实资源专项记为 `REAL_COMPUTE`。验收命令中的 `LIVE_MODEL` 和 `INDEPENDENT_REVIEW` 执行数均为 0。[环回运行器](acceptance/phase2a/regression/loopback.py) · [真实资源专项](acceptance/closure/profile_cases.py)

## 计分和失败续跑

原跨进程场景共用真实稳态和恢复 Run，按所选矩阵项启动需要的场景。P2A-14 同时要求跨进程断言和延迟归约通过。P2A-09 至 P2A-13 从原始批次各自对应的子命令退出码计分。终态协议使用原 204 次工作负载并启用本次用量合同。REG01 引用原矩阵的逐项结果和独立的宽回归操作。双重通知丢失探针单列其观测结果和内层退出码。[跨进程运行](acceptance/phase2a/cross_process.py) · [原协议回归](acceptance/phase2a/regression/run.py)

每次运行使用新目录。失败续跑先校验旧包的外层和逐成员校验和，继承当时的输入位置，选择 `FAIL`、`PARTIAL`、`BLOCKED` 项。支持用显式参数补全本地依赖。每个重跑结果都含新执行的命令及父级清单哈希。已有 ID 会被拒绝。针对全绿运行的失败续跑返回 `NO_FAILURES`，命令列表为空。运行命令的退出码为 0（通过或无失败项）、1（失败）、2（部分完成或本地输入不可用）。[运行与分类实现](acceptance/closure/runner.py)

完整运行中，T03 可以复用同批次的 Python 发现命令回执，继续对原 465 个身份逐项比对。若 T03 仅剩旧失败归因、候选 SHA 和工作代码均相同，失败续跑可从已校验的父包提取该命令的摘要和原始日志。T03 检查源版本、测试环境、离线依赖安装、官方二进制、确切命令及日志摘要，条件不符时独立执行。[T03 回执校验](acceptance/closure/profile_cases.py)

## 证据格式

| 文件 | 复核内容 |
|---|---|
| `environment.json` | Git 分支、SHA、树、修改列表、工作代码变更摘要、系统、架构、执行器和测试 Python/SQLite 版本、输入路径 |
| `commands.json` | 精确 argv、工作目录、环境配置、开始/结束时间、资源配置、是否启动、实际退出码、证据分类、日志相对路径 |
| `summary.json` | 逐项状态、来源、原矩阵与专项数量、父运行、未完成项 |
| `reproduce.json` | 失败复现的完整参数数组和 shell 命令 |
| `raw.tar.gz` | 各探针的 stdout/stderr、内部调用回执、实际 SQLite 快照、事件轨迹、原始时间样本及阶段结果 |
| `raw-files.sha256` | 每个解压后原始文件的 SHA-256 |
| `manifest.sha256` | 外层六个文件的 SHA-256，包括压缩包与内层清单 |

`verify` 会流式解压、逐个检查文件名与内容哈希。加 `--extract-to` 后将校验通过的原件写入新目录。完整运行目录可以复制到取证目录或 Git，命令和 JSON 摘要均可直接读取，数据库及事件原件保存在压缩包内。[归档和验证实现](acceptance/closure/archive.py)

## AC01、AC02、AC03 的实测

三项均执行了公开 CLI。AC01 通过分支回滚与重试用例核对命令、日志、退出码、版本与报告。AC02 解开真实 SQLite 备份，完整性检查得到 `ok`，读取 11 条工作流事件。故意修改内层内容哈希并重新计算外层清单后，`verify` 仍以 2 退出并指出具体文件。AC03 使用缺少指定官方二进制的用例和可正常运行的独立用例构造混合结果，确认重跑只选前者、原文件字节不变、复用旧目录被拒绝，也验证全部通过后没有测试需要重跑。[三项执行摘要](acceptance/closure/automation-history/automation-checked/summary.json) · [原始输出清单](acceptance/closure/automation-history/automation-checked/raw-files.sha256)

开发过程中，AC02 首次用内存反序列化打开带 WAL 标志的已解压备份，SQLite 报错。修复夹具后，使用新文件的只读 immutable 连接进行完整性检查。实际调用 `--failed-only acdb-first` 只执行 AC02，退出 0。初次失败包与后续通过包分别包含 13 个、3 个可校验的原始成员。二者是开发期复现材料，最终冻结候选的整体验收见独立报告。[初次失败](acceptance/closure/automation-history/acdb-first/summary.json) · [精确续跑](acceptance/closure/automation-history/acdb-retry/summary.json) · [Closure 总验收](P2A_CLOSURE_ACCEPTANCE_REPORT.md)

## 冻结版本的整体验收

最终从独立检出的 `3a08a9d1c36759686d29743c75d789cd959848a1` 执行 `run all`。原 Phase 2A 为 15/15，Closure 为 19 项通过和 T03 部分完成，另有 3/3 项补充检查通过。原 465 个测试身份中，461 个通过，4 个沿用基线跳过原因。原始材料只提供另一次执行的失败总数 42，当前仍缺少该次失败的身份和日志。全量包内 345 个文件通过逐项校验，其中 50 份 SQLite 快照解压后通过 `PRAGMA quick_check`，另含 37 份事件轨迹。[冻结版结果](acceptance/closure/runs/frozen-3a08a9d/summary.json) · [完整归档清单](acceptance/closure/runs/frozen-3a08a9d/manifest.sha256)

同一冻结版本执行 `--failed-only frozen-3a08a9d` 后，只启动 T03，耗时 0.697 秒。它复用已校验的父包发现日志，逐项核对原有用例，并保留了资料仍待补齐的结论。新归档包含 11 个原始成员，记录父包 manifest 的完整 SHA-256。[续跑结果](acceptance/closure/runs/retry-3a08a9d/summary.json) · [命令和耗时](acceptance/closure/runs/retry-3a08a9d/commands.json) · [全部运行索引](acceptance/closure/runs/index.json)

中间冻结版 `fa8bba9` 记录了子任务 HTTP 请求迟于父回合结束到达时的计数误判。使用同一官方宿主控制请求顺序后，父回合结束、提前提交被拒、委派的子任务结算、后续回合完成四个时点分别有 2、2、3、4 个请求。修正后的探针从子任务结算后计数，核对只有一个新增父请求，并保存每个阶段的请求 ID 和归属。旧检查在这一时序下失败，两个额外的负对照也验证探针可拒绝重复请求和归属于子任务的请求。[受控对照](acceptance/closure/child-request-order/comparison.json) · [原失败包](acceptance/closure/runs/frozen-fa8bba9/summary.json)
