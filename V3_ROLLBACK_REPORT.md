# GX-H v3 P1 快照与 v2.1 状态回退

本报告适用于独立 P1 附件 `gx-h-v3-p1-linux-x86_64.tar`，SHA-256 为 `d836e34d131cac4dfc3616950255d5fd4cd3c792da50dbf5c83664cf48ba7295`。使用单独状态的新装 P1 可以暂停并保留原目录，再从原 v2.1 安装打开原 v2.1 项目。若候选曾写入旧项目，请使用升级前保留的完整一致副本恢复。[部署与校验](V3_DEPLOYMENT_REPORT.md)

## 已实测的恢复边界

| 观测 | 证据 |
|---|---|
| 此附件新解压后打开原 v2.1 暂停快照的副本 | `gxr_01m825ktkpt3ewxtdm` 为 `PAUSED`，关联 3 个历史 Job、3 个已计量原生 turn 和 32 个已发布 Artifact。公开 `status` 前后 GX 库 SHA-256 同为 `35ac81b908ae1c01bdd8289e23533b839e3addaf6044b6fc41f295affe7a2b2b`，完整性正常、外键错误 0、新模型调用 0。[附件实测](acceptance/v3/p1/standalone-deployment.json) |
| 旧软件和旧数据一起恢复 | 来源提交 `a05dcef`。在较早 P1 软件 `3362256` 对旧库可写打开后，恢复原软件、项目及 Codex 副本。原 v2.1 的公开 `status` 与 `run` 都返回 `PAUSED`，额外环回请求 0。再由当时的 P1 恢复完成后，原 3 个 Job 的回执相同，最终唯一 Job 为 4 个，剩余预留全部为零。[完整备份与恢复回执](acceptance/v3/p1/legacy-checkpoint.json) |
| 此附件对应的产品提交冷恢复 v2.1 | 另一份由 v2.1 `61ed006` 启动的 Run `gxr_01m82eg93hzk0s4xgg` 在 `2476f2b` 下经 `status`、`resume`、终态 `run`。结果为 `COMPLETED`、4 个 Job、58 个 Artifact、7 个 native turn、1,400 个环回 tokens。终态重放额外请求 0。[该提交的完整回执](acceptance/v3/p1/paired-grid-2476f2b.json) |

上述两次执行演练均有此前保存的独立回执。本次打包重新执行了只读打开和新项目启动，沿用已保存的恢复实测。不同 v3 组合的回退及两版本跨进程恢复列入 P3 验收。[阶段状态](V3_ACCEPTANCE_REPORT.md)

## 操作员恢复步骤

1. 记录原 v2.1 安装、项目及专用 `CODEX_HOME` 的绝对路径、软件提交或附件摘要、Run ID 与已知在途 Job。使用原 CLI 的 `stop --state DIR --run-id RID` 暂停相关 Run。等待 Driver、App Server 和在写入这些目录的进程排空。对已启动的外部 Job 按原调度引用核对结果和费用，保留其启动、执行和终态回执。
2. 在静止窗口同时备份旧安装、整个项目树与对应的私有 Codex 状态。以 SQLite backup API 复制 `gx.db` 及 Codex 各 SQLite 库，复制其余普通文件和链接，包括 Artifact 字节、`jobs/` 的输入输出和 sidecar、`runs/`、`threads/`、原生 rollout 及线程元数据。备份保存在私有位置。对 GX 副本执行 `PRAGMA integrity_check` 与 `PRAGMA foreign_key_check`，记录 Artifact 摘要、Job 回执、工作流固定版本、原账及 Run 状态。[同一复制方法](acceptance/v21_staging.py) · [备份清单](acceptance/v3/staging/cold-baseline.json)
3. 回退时暂停并排空候选，把它的整个状态另存，以便核对升级后已发生的实验和费用。在原绝对路径恢复 v2.1 软件、完整项目副本及 Codex 副本，重新选择旧程序及原生客户端。实际演练使用同一绝对路径，因为 Job 和原生会话可能引用这些路径。[恢复程序](acceptance/v3/staging/replay_checkpoint.py)
4. 使用旧安装的 `bin/gx-p0 status --state DIR --run-id RID` 冷开，按备份核对状态、图摘要、原预算、Artifact 及 Job 幂等键和调度回执。对演练中的暂停点，旧 CLI 的 `run --state DIR --run-id RID --timeout 10` 仍应显示 `PAUSED`。在确认备份点以后发生的外部工作已经对账后，再以 `resume` 恢复该 Run。[冻结 v2.1 控制回放](acceptance/v3/staging/control-replay.json)

对于旧软件不能解释的 v3 新 Run，保留其原 v3 安装、包摘要和独立状态，在相应版本继续读取与执行。旧项目回退复原的是备份点，晚于该点的实验结果及账目仍属于另存的候选状态。
