# Phase 0 · GX-H-2 测试基线

2026-11-22 UTC。发布代码在补齐本地依赖和迁移历史后，Python 全量测试 **168/170 通过，2 项失败，0 项跳过**。两项失败都发生在崩溃恢复夹具的准备阶段，未触发其预定的进程故障。Node 独立测试 2/2 通过，宿主配置生成器检查通过。

## 实测结果

| 执行环境 | 测试 | 通过 | 断言失败 | 错误 | 跳过 | 退出码 | 耗时 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 仅发布树，Python unittest discovery | 170 | 160 | 2 | 4 | 4 | 1 | 46.394 s |
| 补齐依赖与 Git 历史，再跑同一套 discovery | 170 | 168 | 2 | 0 | 0 | 1 | 95.792 s |
| 装有锁定依赖，Node 插件宿主 | 2 | 2 | 0 | 0 | 0 | 0 | 0.164 s |

两次 discovery 均覆盖发布树中的 29 个 Python 测试模块。完整输出见[原始日志](acceptance/runs/baseline-clean/unittest.log)、[补齐先决条件后的日志](acceptance/runs/baseline-with-prerequisites/unittest.log)、[Node TAP](acceptance/runs/baseline-with-prerequisites/node-plugin-host.log)。生成器输出与各进程退出码见[生成器](acceptance/runs/baseline-clean/generated-hosts.log)、[首次运行](acceptance/runs/baseline-clean/exit-codes.txt)、[再次运行](acceptance/runs/baseline-with-prerequisites/exit-codes.txt)。

## 可重复的断言失败

`test_committed_acquire_survives_driver_sigkill` 和 `test_committed_finish_survives_driver_sigkill` 在两次运行中均返回 `SCHEMA_INVALID/unobserved_native_subject`。公共夹具创建的 Root 主体为 `test-coordinator`。两个用例在登记模块成员时传入 `native-coordinator`。GX 在进入 `driver.start()` 前拒绝登记。因此这组用例没有测到 acquire 或 finish 提交后丢失回执时的恢复行为。[Root 夹具](gx-h/tests/test_module_collaboration.py) · [崩溃测试](gx-h/tests/test_native_crash_receipts.py) · [两次失败的完整回溯](acceptance/runs/baseline-with-prerequisites/unittest.log)

## 首次运行的环境差异

- 两项 `test_plugin_evaluator` 错误。Node 找不到 `@deepseek-ai/cordis`。复制本地已安装的四个 Node 依赖并核对版本与 `package-lock.json` 后，两项通过。[版本记录](acceptance/runs/baseline-with-prerequisites/versions.txt)
- `test_v2_fencing.BaselineMigration` 和 `test_v2_rollback.Rollback` 各报一项错误。前者读取历史提交 `23d581c27f46e65324246dff269b6ce77c9b3e9a` 的合约，后者克隆本地 GX 根仓库并在旧版、候选版间切换。发布归档没有 Git 历史，发布仓库也没有该旧对象；使用本机独立的原始 GX 历史后两项均通过。[迁移和恢复的实测日志](acceptance/runs/baseline-with-prerequisites/unittest.log)
- 可选 DSH 有四项跳过。复制本机已安装的 SDK 与 runtime-bin（均为 `0.2.2rc2`），从该环境执行 discovery 后，四项都运行并通过。DSH 冷恢复测试记录了未关闭文本流的 `ResourceWarning`。[版本记录](acceptance/runs/baseline-with-prerequisites/versions.txt) · [运行日志](acceptance/runs/baseline-with-prerequisites/unittest.log)

## 来源和重放

初始代码来自发布提交 `4ff89fa93b2f0f9c33fc59a45ad09ddfa17c59b7` 的 `gx-h` 子树 `e631154299192ccbf32111550c223f7642161a2e`。包内初始 `gx-h/` 和首次测试工作区分别用 `git archive` 从同一对象提取。核对了测试工作区全部 286 个跟踪文件的 Git blob 哈希和可执行位。核对结果见[发布快照记录](acceptance/runs/baseline-clean/release.json)。

补齐环境时，在另一临时目录本地克隆 `GX-H-V2/gx-h` 的 Git 历史，检出 `573739bbd3dc51564de95dbd5887b44b8701b10a`，然后覆盖提取同一发布子树。该历史提交与发布子树只有 `runtime/codex-host/README.md` 有差异。回滚用例的内部 checkout 使用原历史提交；其他用例在覆盖发布树之后运行。依赖复制到临时目录，包内只保留发布代码及测试记录。[源码比较](acceptance/runs/baseline-with-prerequisites/extraction-vs-lineage.txt)

在工作区根目录可用以下命令重放。测试使用 Python 3.11.9、SQLite 3.45.3 和 Node 22.17.0。HTTP/HTTPS、SOCKS 环境均指向 Clash，两项本地端口连通性检查通过，HTTP 录制服务使用回环地址，依赖取自本地。

```bash
export HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890
export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY"
export ALL_PROXY=socks5h://127.0.0.1:7891 all_proxy=socks5h://127.0.0.1:7891
export NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1
export PYTHONDONTWRITEBYTECODE=1
scratch=$(mktemp -d)
release=4ff89fa93b2f0f9c33fc59a45ad09ddfa17c59b7
mkdir "$scratch/clean"
git -C gx-h-2 archive "$release:gx-h" | tar -x -C "$scratch/clean"
(cd "$scratch/clean" && python3 -B -m unittest discover -s tests -v)
(cd "$scratch/clean" && python3 -B adapters/hosts/generate.py --check)

history=GX-H-V2/gx-h
git clone --local --no-hardlinks --no-checkout "$history" "$scratch/with-prerequisites"
git -C "$scratch/with-prerequisites" checkout --detach 573739bbd3dc51564de95dbd5887b44b8701b10a
git -C gx-h-2 archive "$release:gx-h" | tar -x -C "$scratch/with-prerequisites"
cp -a "$history/runtime/plugin-host/node_modules" "$scratch/with-prerequisites/runtime/plugin-host/"
cp -a "$history/runtime/dsh-sdk/.venv" "$scratch/with-prerequisites/runtime/dsh-sdk/"
(cd "$scratch/with-prerequisites" && runtime/dsh-sdk/.venv/bin/python -B -m unittest discover -s tests -v)
(cd "$scratch/with-prerequisites" && node --experimental-strip-types --test runtime/plugin-host/host.test.ts)
```

这些基线测试覆盖受控协议对端、本地子进程与临时 SQLite。独立的 Codex 运行时探针、模型行为与 P0 故障场景另由运行时验收记录。上表只统计这里实际运行的命令。
