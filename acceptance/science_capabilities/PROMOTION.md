# 科研插件候选版部署与封存报告复验

供 PI 使用的部署 `gx-v-6/science-capabilities-release` 实际运行源码为 **`a517af40231cb47b5538685955d6999d66ca81b2`**。三次升级均使用原路径的 `git merge --ff-only`，顺序为 `ebb5ec0 → 29c4ff8 → a72f755 → a517af4`。各次均确认父、子 Goal 已封存且状态目录没有活动 Driver。回执的 `first_promotion` 和 `second_promotion` 字段保留前两次发布结果。部署目录的 `git rev-parse HEAD` 给出运行提交。[三次部署回执](evidence/deployment.json)

最终候选修复 Lead 在本回合提交结题时对刚收到的审阅记录的引用。三个变更的运行源码文件已在现场逐字节与候选提交核对。在部署的私有环境中重新执行 `tests.test_source_decision_receipts`，5 项回归通过，覆盖准确引用、新观测到达和跨线程边界。原派生插件及目录摘要均已复核。

部署 `a72f755` 时，`gx-p0 research` 对真实旧库 `p0_provider_limits` 添加了 `refresh_after REAL`。原有 1 行的 8 列逐值相同，新列初值为 NULL。当次的两次重导后均核对了迁移账本与发布 SQL 的摘要。本次 `a517af4` 重导后再次确认前次迁移记录、表结构及原记录的哈希一致。父题仍有 3 个图节点，后继题仍有 2 个，两题各自的图版本数也保持原值。升级前数据库副本保存在本机私有工作目录的 `before-a72f755.db`。

部署保留了研究期间安装的 `org.gx.record-coverage-8902880ed9d712bf6baa5899@1.0.0` 及其目录注册。原地升级前后，该插件的 5 个文件与目录配置字节一致。安装目录中的 23 个包均与各自注册摘要相符。三个候选使用相同的运行依赖锁及插件宿主源码。首次部署时，固定版本的 Codex CLI 0.169.0 可执行文件与发布锁的 SHA-256 相同。Node 22.17.0、npm 10.9.2 和四个 Cordis 依赖通过版本核对，实际安装路径运行的两项宿主测试通过。私有 CPython 3.11.9 中的 Biopython 1.88、NumPy 1.26.4 和 pypdf 7.2.0 均来自该虚拟环境，对应的离线 wheel 与锁定摘要一致。Codex 在打印版本时报告跨文件系统清理旧临时目录的警告，命令退出码为 0。

| 已封存的研究 | 既有 Job | 既有模型回合 | 正常入口重导 |
|---|---:|---:|---|
| 父题 `gxr_01m8d9xyaych3h1z2b` | 2 | 6 | `REPORT.md` 与 `DECISION.md` 前后逐字节一致 |
| 后继 `gxr_01m8daq09da52k64ar` | 2 | 3 | `REPORT.md` 与 `DECISION.md` 前后逐字节一致 |

两次重导复用同一工作区 `gx-science/capabilities-check/live-record-successor/state`，各自新增 0 个 Job、0 个模型回合，两条原结题决定均由普通科研入口返回。本次验收覆盖升级兼容性。原始真实模型与 CPU 运行证据见[跨题配方验收](NATIVE_RECIPE.md)。

在部署目录执行以下命令可重复重导。

```bash
state=../gx-science/capabilities-check/live-record-successor/state
for run in gxr_01m8d9xyaych3h1z2b gxr_01m8daq09da52k64ar; do
  GX_CODEX_OFFLINE=1 .venv/bin/python -B bin/gx-p0 research --state "$state" --run-id "$run"
done
```

如需复核上一个发布，可在另一部署目录检出 `a72f755c03ae5ab252f6a3022cadab00b9836231`，按原包摘要安装派生插件，再以该版本签发的所有者标记恢复封存状态副本。如需复核加列前的版本，可配对使用 `29c4ff8` 与本机私有快照 `before-a72f755.db`。当前运行目录和会话可继续保留供对照。[冷恢复步骤](MIGRATION.md)
