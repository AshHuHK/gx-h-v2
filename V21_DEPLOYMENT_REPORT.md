# v2.1 staging 部署

候选 `a05dcef64522fbbbc12120b1d78da518e6886b10` 已从 Git 导出到独立 staging。在 `var/v21-staging/a05dcef/install` 部署的公开命令将 v2 的 `science_grid@2` 项目 `gxr_01m81ta2sg5fjtbqkf` 冷恢复至 `COMPLETED`。恢复耗时 5.866 秒，包括继续研究、正式 App Server 协议交互及剩余 CPU 工作。原本在途的第三个 Job 使用原启动记录和落盘结果完成对账。原来两个已接受的 Task、已有决策和已结费用都保留。[逐命令回执](acceptance/v21/deployment/staging.json)

| 观测 | 升级前的真实 v2 | 完成后的 v2.1 |
|---|---|---|
| 程序提交 | `2314eed72822387e03378e8e676ad5fa9228a8d0` | `a05dcef64522fbbbc12120b1d78da518e6886b10` |
| 可执行代码树指纹¹ | `d00064da5f0daf485ad547bbe6c64ed9a47838bfc9604ba016ac68304b9036a7` | `91ee51f3cb9f02e1b601b1d098006733b0c984e14706f11fe87f7b0c34788464` |
| 研究状态 | PAUSED，原库仍登记一个在途 Job | COMPLETED，生成 `gxa_01m81taz5ev3kvbkch` |
| 已接受 Task / 独立提交 | 2 / 2 | 6 / 6 |
| Job / 原生 Turn / 决策轮次 | 3 / 3 / 1 | 4 / 7 / 3 |
| Artifact / 已结账事件 | 32 / 9 | 58 / 23 |

¹ 指纹覆盖 `bin`、`gx-h`、`examples`、`codex_adapter` 中的文件名、可执行位和内容或链接目标。候选安装还含完整提交的文档、测试和离线输入。原始库、模型配置与 Job 目录均在本次新建的 staging 中。[部署程序](acceptance/v21_staging.py)

## 运行与账目

真实 Linux x86_64 Codex 0.169.0 App Server 通过 stdio 接受 GX 调用，独立的本机 Responses 测试服务提供固定、可对账的回答。四个 CPU Job 由 GX 的原 Job owner 启动和回收，载入真实数值脚本。原预算 `gxb_01m81ta2sgqzb8gmxb` 结算 1400 tokens，与逐线程官方累计之和以及回环服务提供的 40 次响应一致；原生调用 7 次，Job 额度使用 4 次，最终各项预留归零。终态重开新增模型请求 0，研究和原账快照逐项相同。这组记录用于验证调度、升级和计量，科学结论来自合成有限网格。[项目与账本](acceptance/v21/deployment/staging.json)

主 checkout 另行用 `bin/gx-p0 doctor --smoke` 通过真实 ChatGPT OAuth 推理，观察到模型 `gpt-6.2-astra`、effort `xhigh`、16,206 个原生 tokens、墙钟 7.137 秒。该回执验证在线接入；上表的升级、计量与回退仍使用可复现的独立本机对端。[真实 OAuth 回执](acceptance/v21/deployment/oauth-smoke.json)

## 在新 checkout 复现

在含上述两个 Git 对象的完整 checkout 中运行。Linux 需具备 CPython 3.11、SQLite、Git、Node 22.17、npm 和已配置的本机 Clash。离线正式 Codex npm 归档位于 `release-assets/codex-0.169.0-linux-x64.tgz`，SHA-256 为 `89801b4b97faba562eb80fe0a833159ac79399be674022f4b1a0ccab1d153faa`。原 v2 发行物中的同名成员已在搬入新 checkout 前后校验。本机新环境为 CPython 3.11.9、SQLite 3.45.3、Biopython 1.88、NumPy 1.26.4。所有安装使用仓内归档或固定 wheel。[离线锁定清单](release-assets/lock.json) [实测环境](acceptance/v21/deployment/environment.json)

```sh
python3 -m venv --copies .venv
.venv/bin/python -m pip --isolated --disable-pip-version-check install \
  --no-index --no-cache-dir --require-hashes --find-links release-assets/python \
  -r release-assets/python/requirements.lock
.venv/bin/python -B scripts/release_assets.py all
work_base=$(mktemp -d)
.venv/bin/python -B acceptance/v21_staging.py --candidate a05dcef64522fbbbc12120b1d78da518e6886b10 \
  --work "$work_base/stage" --output "$work_base/staging.json"
```

复验会先从 `2314eed72822` 原代码建立真实项目，再从指定候选提交的完整归档安装第二套 Codex、Schema、Cordis 及私有 venv。工作目录须尚未存在，便于保留当次数据库、原安装、回退副本、启动回执和验收观测。此前的 47165af / 23d581c fixtures 仍用于各自的历史回归。[历史数据库实测](V21_ROLLBACK_REPORT.md)

本次留存的 staging 已在卸下测量钩子后用公开 CLI 再次确认同一终态。Shell 保持在仓库根目录，可直接查询。[最终安装与文件校验](acceptance/v21/deployment/verified.json)

```sh
staged="$PWD/var/v21-staging/a05dcef"
PATH="$staged/venv/bin:$PATH" "$staged/install/bin/gx-p0" status --state "$staged/project"
```

Staging 中包含上游源码文件 10235 个，锁定 commit `d32063f96c2dff1dc96af839b3952abb6acfeda4`，上游 LICENSE、NOTICE 及 1,430 个 experimental Schema 文件。已验证正式 ELF SHA-256 `b452ca20f838a8855a88a41050ee4a694033fff4ea3ecdad5633e6e0e8d13f2b`；可选 Cordis 用随包 npm 依赖实跑 2 项测试。部署按 CLI 调用运行，回环模型服务随验收进程结束。本次部署操作的范围为新建 staging。[运行时依赖闭包](V21_DEPENDENCY_CLOSURE.md)
