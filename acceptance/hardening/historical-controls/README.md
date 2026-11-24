# 78a8b07 两个断言的同源码对照

2026-11-24 17:38:55–17:39:09 UTC，在独立 checkout `78a8b07766376d93a458e5175c41f1f327ff8f36` 运行原版两个 unittest。每个用例各运行两次，使用同一个临时 Python 环境、离线锁定的 Node 包及真实本地子进程。网络命名空间没有对外路由，loopback 上运行拒绝全部请求的 Clash。源码和测试的 Git 差异为空。[原始命令、四次输出与校验清单](controls-78a8b07.tar.gz)

| 原测试方法 | 单一变量 | 负对照 | 正对照 |
|---|---|---|---|
| `test_p0_resource_dimensions.ResourceDimensions.test_specific_job_refusal_survives_resume_until_the_project_phase_changes` | 准入进程读到的 `cpu.max` 从 `200000 100000` 改为 `400000 100000` | 上限 1 线程，已占 1、剩余 0。原 Job owner 在 `service.py:747` 抛出 `RESOURCE_DENIED`，规则为 `cpu.threads_free`。数据库有 2 个 Job，第 52 行报 `2 != 3`，退出码 1。 | 上限 2 线程，已占 1、剩余 1。数据库有 3 个 Job，原测试逐个等到 `SUCCEEDED`，退出码 0。 |
| `test_tl1a_review_job.ReviewJob.test_scientific_disagreement_and_unavailable_fit` | 在同一个 venv 安装锁定的 Biopython 1.88 wheel，NumPy 1.26.4、npm 10.9.2、Cordis 4.1.0 和 CPU 保持一致 | `review_batch.py` 子进程返回 `unavailable`，具体错误为 `ModuleNotFoundError: No module named 'Bio'`。第 82 行报 `'unavailable' != 'disagrees'`，退出码 1。 | 相同观测和标准产生 `disagrees`，其中数值容差检查为 false。原测试继续更换来源哈希，得到预期的 `unavailable` 和来源不符错误，退出码 0。 |

## 与历史日志的对应关系

原运行记录于 09:17 UTC，分类文件登记了 Python 3.13.18、SQLite 3.53.1、Node 22.23.5、两个逻辑 CPU，以及缺少 Bio 和 Cordis。本次使用 Python 3.11.9、SQLite 3.45.3 和 Node 22.17.0。实验检验了每项因素是否足以产生原现象。[原分类和环境记录](../../../gx-historical-42-classification.json)

Job 用例的原始日志第 29–71 行已经记录了提交被拒绝的调用栈。续跑进入 `run_control → advance_resumed → experiment → submit_geometry → submit_workflow_job → JobService.submit → _admit → _threads_or_refuse`，错误原文与本次低容量结果相同。第 72 行起始的 `F` 是进度流中第一个失败记号，之前有 194 个已完成记号。冻结源码的 discover 顺序也将该方法列为全部 465 项中的第 195 项。首个 FAIL 标题和断言在第 1851–1857 行。旧机的原始 `cpu.max` 字节未附。此次只替换准入进程读取该文件的值，其他 cgroup 读数、GX 配额算法、占用统计和 Job 执行均走原代码。当前宿主实际读数为 `2400000 100000`。[原始运行日志](../../../gx-historical-42-failures-78a8b07.log)

reviewer 原始日志第 1957–1965 行显示状态断言。归档材料中没有当时的 `checks.json`。包内 `review-no-bio.log` 的具体导入异常来自本次评审子进程写入的结果文件，`review-bio.log` 给出相同观测与标准的 SHA-256。其他可导致 `unavailable` 的情况仍然存在，原测试本身就覆盖了来源哈希不符。这两组对照只涉及上表两个历史用例。[原始运行日志](../../../gx-historical-42-failures-78a8b07.log)

## 复核

证据包 SHA-256 为 `8113f038f22540a4af3550462d2be31537bcf0574db74ca07b2925c1fe18630f`。解包后执行 `sha256sum -c SHA256SUMS` 可核对每个文件。`setup.log` 保留实际执行的命令和四个子进程的退出码，`probe.py` 提供观察和 CPU 读数替换，`network.json` 为离线命名空间收据。`observations.json` 保存被冻结源码、wheel、npm 归档、原始日志和分类文件的哈希，以及四次运行的关键读数。[下载证据包](controls-78a8b07.tar.gz)
