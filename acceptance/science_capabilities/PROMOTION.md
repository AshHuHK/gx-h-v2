# 科研插件候选版部署与封存报告复验

生产工作树 `gx-v-6/science-capabilities-release` 的实际运行源码为 `29c4ff8e29681d593e48da22fc650fed86eb47a5`。部署在原路径从 `ebb5ec0ebe07295c673412f8d0a52850aef87e7b` 执行 `git merge --ff-only`。升级时取得状态目录的独占锁，两项研究均已封存，9 项任务已获接受，4 个 Job 均已成功。[部署回执](evidence/deployment.json)

部署保留了研究期间安装的 `org.gx.record-coverage-8902880ed9d712bf6baa5899@1.0.0` 及其目录注册。原地升级前后，该插件的 5 个文件与目录配置字节一致。安装目录中的 23 个包均与各自注册摘要相符。固定版本的 Codex CLI 0.169.0 可执行文件与发布锁的 SHA-256 相同。Node 22.17.0、npm 10.9.2 和四个 Cordis 依赖通过版本核对，实际安装路径运行的两项宿主测试通过。私有 CPython 3.11.9 中的 Biopython 1.88、NumPy 1.26.4 和 pypdf 7.2.0 均来自该虚拟环境，对应的离线 wheel 与锁定摘要一致。Codex 在打印版本时报告跨文件系统清理旧临时目录的警告，命令退出码为 0。

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

如需复核升级前版本，可在另一部署目录检出 `ebb5ec0ebe07295c673412f8d0a52850aef87e7b`，按原包摘要安装派生插件，并使用该版本签发的所有者标记恢复一份封存状态副本。当前运行目录和会话可继续保留供对照。[冷恢复步骤](MIGRATION.md)
