# v2.1 升级与回退

真实 v2 提交 `2314eed72822387e03378e8e676ad5fa9228a8d0` 创建了项目 `gxr_01m81ta2sg5fjtbqkf`。在原 Driver、App Server 已退出、三个已获准 CPU Job 均已写出终态回执、第三个 Job 尚待 GX 入账时，为原项目及原生线程状态建立一致副本。v2.1 原地应用了 8 个新增 P0 migration。恢复副本后，同一目录中的原 v2 程序通过公开 `status` 和 `run` 重开 `PAUSED`，模型请求增加 0。随后 v2.1 从这份原 v2 状态完成剩余工作。[逐步证据](acceptance/v21/deployment/staging.json)

| 检查点 | 实测 |
|---|---|
| 原状态 | `science_grid@2`，Task 2 个已接受，原生已结 Turn 3 个，Job 3 个，原决策 1 轮 |
| 正在写入的来源 | 驱动在公开 `stop` 后退出；CPU 进程在备份前写出每个 Job 的 `outcome.json`。保留既有 launch.claim、runner.json 和 outcome.json 的 SHA-256 |
| SQLite 在线备份 | 提前打开的只读事务仍见 `WAITING_EXTERNAL_JOB`，源 WAL 为 2,595,632 字节，`sqlite3.Connection.backup` 得到 `PAUSED` 及全部本轮结算。原项目和从副本恢复的数据库均通过 integrity_check 与 foreign_key_check |
| 原始项目副本 | SHA-256 `b1f6e4d4ec953fbc8b3603ab78c65baa0270bc0b059d0bd78ca48382775e33a7`，在原路径恢复后逐字节校验 |
| Codex 本地状态 | 6 个 SQLite 库逐库 API 备份并通过 quick_check；其余普通文件及线程记录随副本保存。临时锁与对应 WAL/SHM 由 SQLite 处理 |
| 候选写入 | 真正执行可写 CLI `stop`，原库 inode 不变；原 12 个 migration 标记及其摘要保留，原预算 spent、reserved 和所有旧结算事件不变 |
| 对旧版的恢复 | 切换回同一绝对安装路径、复制原 GX 与 Codex 备份到原绝对状态路径。v2 可重新打开，恢复后 schema 与旧版相同，Task、租约、Root、提交、Job、原账、所有 32 个已知 Artifact 摘要和 Job 回执逐项相同 |
| 最终冷恢复 | 第二次切入 v2.1 后同一 run 完成。原三个 Job 的幂等键、scheduler_ref、启动 claim、runner 身份和真实计算结果保持相同，最终仅有 4 个唯一 Job |

## 操作顺序

1. 保存原软件版本和固定安装路径。在该项目上公开 `stop`，等 Driver 排空原生 turn，等待已启动 Job 的权威终态回执。保留原运行号、外部调度引用、源 Artifact 和预算引用。外部计算仍可能推进的机器应先完成对账。
2. 在无写者的窗口使用 SQLite backup API 复制 `project/gx.db` 及 `CODEX_HOME` 中的 SQLite 库。其余 Artifact、Job 目录与原生线程状态随同复制，对备份运行 integrity_check 与外键检查。保留旧安装、源数据和副本。
3. 把候选放入相同的正式路径，对原数据库执行可写的公开命令。在允许新的外部工作前记录新增迁移和原账核对结果。本次在该窗口执行了回退。
4. 将候选及已迁移数据库分别移入保留目录，再把旧安装放回原绝对路径，用一致副本恢复项目与原生线程目录；比较备份哈希并用原公开 CLI 冷开。恢复只覆盖该时间点，后续外部任务和费用必须先清点保留。本次回退窗口产生的新外部调用为 0。
5. 实测最后从恢复好的 v2 状态再次切换到候选，用 `resume` 消费第三个 Job 已落盘的回执。Job 的外部执行次数和原预留均已核对。[可运行的回退复验](acceptance/v21_staging.py)

正式候选还在已部署的 `install/gx-h` 运行 3 个启动恢复专项，覆盖创建前超时、已创建 Run 的重试，以及无 run_key 的原 Intent 已写入但尚无 checkpoint、另有 keyed Run 的混合工作区。测试用真实 SQLite 和 Job owner 注入开始阶段故障，三项通过，回环模型请求 0 次。这三个定向用例补充了上表使用原 v2 程序构造的已运行项目。[最终安装上的专项回执](acceptance/v21/deployment/partial-start-regression.json)

## 本机留存与命令

| 相对 `var/v21-staging/a05dcef/` | 用途 |
|---|---|
| `original-install/` | 原 v2 归档导出的 GX 程序 |
| `v2-backup/project/`、`v2-backup/codex/` | 实际用于回退的时间点副本 |
| `retained-upgraded-project/`、`retained-upgraded-codex/` | 第一次候选迁移后的保留副本 |
| `install/`、`project/`、`codex/` | 正式 staging 在 v2.1 完成后的状态 |
| `runtime/codex`、`venv/` | 独立锁定 ELF 与离线 Python 依赖；候选另有随包 ELF |

```sh
stage_base=$(mktemp -d)
.venv/bin/python -B acceptance/v21_staging.py --candidate a05dcef64522fbbbc12120b1d78da518e6886b10 \
  --work "$stage_base/staging" --output "$stage_base/rollback.json"
```

该命令在空路径实跑两代公开 CLI、离线模型对端和真实 CPU Job，可在其他机器生成新的 Job、线程和数据库哈希。本机原始回执中包含实际原库、迁移标记、任务、预算、Job 身份和摘要。生产系统和真实外部任务均未参与本次回退。[部署与环境](V21_DEPLOYMENT_REPORT.md)
