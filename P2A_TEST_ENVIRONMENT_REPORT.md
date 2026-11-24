# P2A 测试环境与 465 个用例的复核

**T01、T02 和真实资源准入通过。T03 为 PARTIAL。** 在产品修订 `c3b7ae18094c9c08cf13cbc360c496af461309ce` 的固定环境中，原 465 个 Python 用例有 461 个通过，4 个沿用原有跳过原因，缺失、错误、失败和新增跳过均为 0。用户提供的闭包包记载另一轮运行有 42 项失败，未给出该轮的用例身份或原始日志。此次可核对 465 个身份，能关联到那 42 项的原始记录为 0。[本次运行摘要](acceptance/closure/profile-history/20261124T111105Z-ebd13f73e167/summary.json) · [原 465 项的逐项日志](acceptance/phase2a/regression/final-v2/batch/suite/python-full.log)

| 场景 | 实测结果 | 证据类别 |
|---|---|---|
| T01 固定容量 | 将进程 CPU 亲和性分别设为 1 和 224 个可用核，两次均观测到 2 个逻辑 CPU、2 核 quota、2 个可用 Job 线程、0 个 GPU。两个单线程 Job 占满容量，第三个获得 `RESOURCE_DENIED`，取消一个后同一第三项准入成功。夹具在准入事务之后、分叉之前暂停启动。 | SCRIPTED |
| T02 客户端检测 | 父进程带有 `CLAUDECODE`、`CLAUDE_CODE_*`、`CLAUDE_PROJECT_DIR` 及额外哨兵。隔离子进程通过 `gx_run_start` 启动并保存 `other_verified`。专门传入标记的对照子进程识别为 `claude_code` 并收到 `CLIENT_CAPABILITY_MISSING`。 | SCRIPTED |
| T03 历史身份 | 真实执行原 `unittest discover -s tests -v`，耗时 166.378 秒。逐个匹配原修订的 465 个身份和跳过原因。历史 42 项的逐项归因待该次原始证据。 | SCRIPTED |
| 真实资源准入 | 本机观测 224 个逻辑 CPU、24 核 cgroup quota，生产配置按 0.5 配额比例和 8 线程上限给出 8 个可用线程。`nvidia-smi` 返回 1 个设备。一个真实本地合成 CPU Job 经同一服务准入、启动并成功结束，产生 3 个输出。 | REAL_COMPUTE |

## 资源与环境入口

验收进程通过 `GX_TEST_RESOURCE_PROFILE=deterministic` 选择固定观察值。两核夹具将 CPU 池设为 2、配额比例设为 1.0。容量快照与 `JobService.submit` 都通过 `cpu_room` 扣除同一 store 中已占用的线程。原生产配置在相同两核输入下实际给出 1 个 Job 线程。真实 profile 使用原配置、cgroup 读取和 GPU 探测。测试专门传入的 GPU 解析样本仍使用其自身输入。[资源选择](gx-h/gx_core/config.py) · [CPU 与 Job 容量](gx-h/gx_core/jobs/capacity.py) · [GPU 读取](gx-h/gx_core/capabilities/probe.py)

两个 profile 均从允许的变量名构建子进程环境，使用独立的 HOME、CODEX_HOME 和临时目录。运行器提供指定的 Python、锁定的 Codex 和系统工具路径，固定 locale 与时区。HTTP、HTTPS、SOCKS 使用仓库规定的本地 Clash 端口，大小写变量同步，bypass 取父环境与三个允许的 loopback 名称的交集。剩余宿主变量与用户凭据不会传入。真实资源的子进程保留已有的 CUDA 设备可见性。[环境生成函数](acceptance/closure/profile.py) · [原生回归入口](acceptance/phase2a/regression/run.py) · [Python 套件入口](acceptance/v21/regression.py)

## 逐项结果与来源

用例身份来自 frozen final-v2 的原始 `python-full.log`，SHA-256 为 `1f3b2d106f3b125b8ec5dafaa89ee1a6b4605f25dd88196d64abf52706a483fd`。复跑使用相同的 Python 3.11、哈希锁定的本地 NumPy/Biopython wheels、离线 Cordis 和 Codex 0.169.0。原始日志中的四个跳过是两个可选 DSH activity 用例和两个 DSH runtime 用例。本次逐项结果保存在压缩包的 `T03/evidence/baseline-465.json`，其中每行保留身份、基线结果、实测结果及跳过原因。旧的 42 项登记在相邻的 `original-42.json`，保留预期数量、缺失来源和空的已识别集合。[原依赖摘要](acceptance/phase2a/regression/final-v2/batch/dependencies.json) · [本次原始证据](acceptance/closure/profile-history/20261124T111105Z-ebd13f73e167/raw.tar.gz)

按身份复核能够读取原始 unittest 失败段、pytest 的失败摘要，或包含 `test_id` 及可选 `original_error` 的 JSON 列表。同一测试方法的多个 subTest 保留各自原始标题。保留的 v3.1 失败日志实际包含 6 个失败标题、3 个不同方法，读取器逐项对应这 6 个标题。该历史运行有 428 个测试，故只能用于核验读取器。原始异常和实测对照共同说明环境原因时，可归为 `environment`。JSON 也可携带独立复核给出的 `classification`、`reason` 和 `evidence` 文件，原判与证据摘要会一并保留，支持 `environment`、`fixture`、`regression`、`unresolved`。单独看到这次通过会保留 `unresolved`。[复核实现](acceptance/closure/profile_cases.py) · [真实 subTest 失败日志](acceptance/v31/regression-b2/python-full.log)

## 复跑与校验

在仓库根目录执行下列命令。运行器记录 SHA、dirty 状态、精确命令、退出码、环境和 SHA-256。T03 可复用同一轮、同修订、相同 profile 的完整 Python 发现日志，经核对解释器调用、依赖锁定、Codex 摘要和日志摘要后使用。独立执行 T03 时会在临时 Git 归档中安装锁定的离线依赖并运行套件。已有 42 项原始表时，可向第一条命令提供 `--original-failures /path/to/original-test.log`。[归档清单](acceptance/closure/profile-history/20261124T111105Z-ebd13f73e167/manifest.sha256)

```bash
bin/gx-acceptance run all --case T01 --case T02 --case T03 --case REAL_RESOURCES
bin/gx-acceptance verify 20261124T111105Z-ebd13f73e167 --runs-dir acceptance/closure/profile-history
```

已校验并解包 39 份原始文件。压缩包 SHA-256 为 `55002f3d627f4888cde68da7968c2f66e26f41dde408f715d325a874fd6bcc2e`。两个确定性准入数据库各有 3 个 Job、28 个事件，真实资源数据库有 1 个 Job、30 个事件，三个快照的 SQLite 完整性检查均为 `ok`。[逐文件 SHA-256](acceptance/closure/profile-history/20261124T111105Z-ebd13f73e167/raw-files.sha256)
