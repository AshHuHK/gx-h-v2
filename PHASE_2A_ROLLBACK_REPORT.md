# Phase 2A 静态 Run 回退演练

**PASS（SCRIPTED）。** 冻结候选 `2a9ec17b2b5858737838e816ee9023cafda11de0` 可写重开历史 `science_grid@2` 项目后，基线 `51e0e13583d95e9cad1386e1d4c77324977e97ea` 从升级前保存的回退点重开。七条 CLI 命令均以 0 退出，返回同一个暂停 Run。回退点、候选重开后和恢复基线后三份数据库逐字节一致。演练使用干净的冻结提交和无外部路由的临时网络命名空间。[逐阶段回执](acceptance/phase2a/rollback/receipt.json) · [完整 CLI 输出](acceptance/phase2a/rollback/cli.jsonl.gz)

## 演练边界与方法

原件来自 `a05dcef6` 生成的冻结历史项目 `gxr_01m84f7xhy3n2nqfqe`。随附压缩包保留历史 SQLite、Artifact 原件及三个本机 Job 的侧车。原数据库 SHA-256 为 `d1483558ee628b7a2eb55ae3414848c3bea3b1c42ad8a77b50b639ee7216a860`。复验会先对照既有回执核实原件和业务行，再使用独立临时目录中的软件与项目。[历史回执](acceptance/v31/v2-checkpoint.json) · [离线输入](acceptance/phase2a/rollback/historical-project.tar.gz)

用 `git archive` 分别导出基线和候选。先由基线在空临时目录生成匹配本次安装路径的本地状态标记，再复制冻结数据。首次可写启动补齐原 v3.1 的三项 P0 migration，标记从 20 个增至 23 个。随后通过公开 CLI 执行 `stop`、`run`，在 `PAUSED` 状态用 SQLite backup API 和普通文件副本建立回退点。将候选移到同一安装路径执行可写的 `stop` 和只读 `status`。保留候选及其项目后，恢复原软件、回退点及其本地标记到同一临时绝对路径，使用基线执行 `status` 和两次 `run --run-id … --timeout 5`。全过程未要求 `resume` 或启动宿主。[演练脚本](acceptance/phase2a/rollback/reopen.py)

## 验证项目

| 观测 | 核对方法 |
|---|---|
| 单等待从何处接续 | 历史工作流 pin 和第 13 个 checkpoint 关联同一个 `experiment` 节点。逐项比较 completed、interrupt ref 和整段 state_json 摘要。这个历史格式以节点及中断引用识别位置。 |
| 旧 Job 与在途预留 | 两个 Job 已在 GX 标为 SUCCEEDED。第三个 `gxj_01m84f83q3gxb8qhw4` 在库中仍标为 RUNNING，但本机已有成功退出的 outcome。对照三份 launch.claim/runner/outcome 和幂等键，并核对该 Job 的 `rsv_01m84f83t2cb7yzxkk` 仍保留 30 CPU 秒和一次提交额度。 |
| 原件及结算 | 对照 32 个发布的 Artifact 引用和原始文件摘要、9 个预算结算事件、2 个科研结算、4 个 outbox 项及 118 个原事件。原始预算区分已消费与仍预留的金额。 |
| 数据库和宿主 | 分别获取回退点、候选可写重开后、恢复基线重开后的 SQLite 镜像，核验完整性、外键、schema 及迁移记录。用临时 Codex 启动记录器和 Python 网络调用记录器测量宿主与网络尝试。 |

## 冻结版本实测结果

| 阶段 | CLI 调用顺序 | 观测 |
|---|---|---|
| 基线建立回退点 | `stop`、`run --timeout 5` | 两次均返回原 Run 的 `PAUSED`，迁移标记 23 个。 |
| 候选重开同一项目 | `stop`、`status` | 两次均返回 `PAUSED`，Job、启动认领、Artifact、预算结算、科研结算增量各为 0。 |
| 恢复基线及回退点 | `status`、`run --timeout 5`、`run --timeout 5` | 三次均返回 `PAUSED`，五项增量继续为 0。 |

三阶段的 SQLite 镜像解压后 SHA-256 均为 `b8e4f7baeba668edc7aefbbc6c90ee05a9303251028202d42a86c0edbfd879ee`，因此只存储一份镜像。各阶段的 `integrity_check` 返回 `ok`，外键违规数为 0。checkpoint `gxc_01m84f83y2t3cd6y7g` 的序号为 13，已完成 `proposal`，在 `experiment` 等待 `gxi_01m84f83y193rkj2ht`。三阶段均保有上述 3 个 Job、32 个 Artifact、9 个预算结算、2 个科研结算、118 个事件和 4 个 outbox 项。旧 Job 的预留保持 `HELD`。七次 CLI 调用的 Codex 启动尝试和 Python 网络调用尝试计数均为 0。[SQLite 镜像](acceptance/phase2a/rollback/gx-b8e4f7baeba668ed.db.gz) · [逐阶段字段与摘要](acceptance/phase2a/rollback/receipt.json)

## 同一版本的 Job 状态后端故障演练

**P2A-15 PASS（SCRIPTED）。** 独立 Run `gxr_01m85gj49ky8jh265g` 实际启动两个本机 CPU Job。隐藏整个后端目录时，两个 Job 都推迟对账。仅让 `gxj_01m85gj4e5aj1th20y` 的读取超时时，它保持 `SUBMITTED` 和原预留。同一轮对账把另一 Job `gxj_01m85gj4sehy4anfxp` 按退出码 1 结为 `FAILED`。随后注入损坏的回执，前者进入 `OUTCOME_UNKNOWN` 并保留预留。再次读取原成功回执后，使用同一 Job ID、调度引用和预留结为 `SUCCEEDED`。再次对账的变更数为 0。两个 Job 各有一次预算结算和一条终态 outbox，各自只启动一次。Codex 启动次数为 0。[Job 回执](acceptance/phase2a/jobs/result.json) · [对账指标](acceptance/phase2a/jobs/reconcile-metrics.json) · [启动器原件](acceptance/phase2a/jobs/runner-records.tar.gz)

可以从快照和事件核对上述故障时序及已结账目。产出的 observation 使用合成输入 `x=2`，工具返回 `score=0`。[对账前](acceptance/phase2a/jobs/before.sqlite.gz) · [单个读取延迟期间](acceptance/phase2a/jobs/deferred.sqlite.gz) · [恢复后](acceptance/phase2a/jobs/after.sqlite.gz) · [关联事件](acceptance/phase2a/jobs/events.json) · [工具输出](acceptance/phase2a/jobs/observations.json)

## 兼容边界

**禁止用旧 Engine 在 2A 已产生多等待的库上原地继续。** 基线只读取当前投影的 `pending_interrupt`、`interrupt_ref` 和单节点位置。2A 的 `node_waits` 能同时表达多个有独立 owner、引用和恢复条件的等待。SQLite DDL 相同无法保证旧推进器理解这些状态。已产生新格式的项目保留在匹配的 2A 版本和状态上完成对账、继续或归档。旧版仅接管授权回退点上的历史静态 Run。[基线源码图](PHASE_2A_SOURCE_MAP.md) · [2A 的分支等待读取](gx-h/gx_core/orchestration/branches.py)

1. 通过候选的公开 `stop --state … --run-id …` 停止该 Run 的新工作，等目标 Driver 停止推进。由现有 JobService 清点已准入/已投递的 Job，用各自原 `job_id`、幂等键、`scheduler_ref`、启动/终态侧车和预留对账。对外部状态未知或暂不可查询的工作保留原预留，继续在候选侧按原身份查询。
2. 为数据库使用 SQLite 在线备份，保存项目的 Artifact、Job 目录、checkpoint/outbox、未结用量及原 thread/turn 记录。实际项目如需恢复宿主，同步保存对应原生线程状态、软件/插件 pin 和安装路径。检查 SQLite 完整性与外键，并核对原件、已确认结算及实际外部工作的身份。
3. 如果回退点之后已经提交新 Job、产生费用或结果，保留这些记录和原候选库，由原所有者完成对账。旧快照不能撤销外部执行。核清期间用独立副本检查旧版，保留候选对业务状态的写权，避免从旧快照重新派发。
4. 经核对可接管时，用基线和完整一致副本恢复同一项目，先读取 `status`，再对暂停项目运行有时限的 `run`。核对 Run/pin、Job 及启动回执、Artifact 摘要、已结/未结账目与 checkpoint。需要真正恢复计算时，通过原 Job 的状态查询及既有结算路径接续。
