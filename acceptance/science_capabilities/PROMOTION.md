# 科研插件候选版部署与封存报告复验

供 PI 使用的生产工作树 `gx-v-6/science-capabilities-release` 实际运行源码为 **`a72f755c03ae5ab252f6a3022cadab00b9836231`**。首次部署从 `ebb5ec0` 快进至 `29c4ff8`，随后在原路径用 `git merge --ff-only` 升级到当前候选。两次升级均在父、子 Goal 已封存且未持有 Driver 的条件下进行。首次完整验收保存在回执的 `first_promotion` 字段，当前候选可由部署目录的 `git rev-parse HEAD` 核对。[两次部署回执](evidence/deployment.json)

当前候选的 `gx-p0 research` 对真实旧库 `p0_provider_limits` 添加了 `refresh_after REAL`。原有 1 行的 8 列逐值相同，新列初值为 NULL。两次重导后均核对了迁移账本与发布 SQL 的摘要。父题仍有 3 个图节点，后继题仍有 2 个，两题各自的图版本数也保持原值。升级前数据库副本保存在本机私有工作目录的 `before-a72f755.db`。

部署保留了研究期间安装的 `org.gx.record-coverage-8902880ed9d712bf6baa5899@1.0.0` 及其目录注册。原地升级前后，该插件的 5 个文件与目录配置字节一致。安装目录中的 23 个包均与各自注册摘要相符。两个候选使用相同的运行依赖锁及插件宿主源码。首次部署时，固定版本的 Codex CLI 0.169.0 可执行文件与发布锁的 SHA-256 相同。Node 22.17.0、npm 10.9.2 和四个 Cordis 依赖通过版本核对，实际安装路径运行的两项宿主测试通过。私有 CPython 3.11.9 中的 Biopython 1.88、NumPy 1.26.4 和 pypdf 7.2.0 均来自该虚拟环境，对应的离线 wheel 与锁定摘要一致。Codex 在打印版本时报告跨文件系统清理旧临时目录的警告，命令退出码为 0。

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

如需回退本次更新，可在另一部署目录检出 `29c4ff8e29681d593e48da22fc650fed86eb47a5`，按原包摘要安装派生插件，并使用该版本签发的所有者标记及本机 `before-a72f755.db` 恢复一份封存状态副本。当前运行目录和会话可继续保留供对照。[冷恢复步骤](MIGRATION.md)
