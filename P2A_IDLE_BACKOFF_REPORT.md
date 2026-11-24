# Phase 2A Driver 空闲退避验收

**IDLE01、IDLE02、IDLE03 均通过。** 单 Run 和共享工作区现在按实际进展退避。新消息、原生管道数据、分离式 Job 状态变化都能提前唤醒 Driver。无输入的实时时钟观测持续 5.026 秒，进程 CPU 用时 0.069 秒，期间阻塞等待 6 次。统一入口独立执行三项检查，归档校验通过。[统一验收结果](acceptance/closure/idle-history/idle-final/summary.json) · [专项原始记录](acceptance/closure/idle-history/dc70b1133e4f/raw.tar.gz)

## 实际观测

| 验收项 | 刺激与判定 | 冻结源码首轮 / 同版复跑 |
| --- | --- | --- |
| IDLE01 | 对真实 Driver 逐轮读取计划等待。另在不改时钟的独立探针中观察 CPU 和内核阻塞时间。 | 两轮均依次得到 0.1、0.2、0.4、0.8、1.6、3.2、4、4、4 秒。新活动使下一次无活动等待回到 0.1 秒。实时探针的最后一次计划等待为 1.852 秒，由调用方的 5 秒期限截短。 |
| IDLE02 | 在两个 CLI 模式都将进入长等待时，让另一进程通过 `operator.submit` 提交暂停。另用真实子进程、分离式 CPU Job 和操作系统信号触发唤醒。 | 两种模式两轮均在提交后 0.2 毫秒返回等待，并实际执行暂停。Job 从启动返回至观察到 `outcome.json` 分别为 0.454 / 0.453 秒，包含命令自带的 0.3 秒等待。两个模式每轮均收到 SIGINT、SIGTERM，对应退出码 130、−15，发送到观察退出约 31.5、3.3 毫秒。 |
| IDLE03 | 固定业务时钟，让 Job 和 Lease 依次到期。另用实时时钟，在已暂停且原生连接离线的 Run 上等待 Usage 宽限期。 | Job 的 180 毫秒期限在 181 毫秒处理，Lease 的 420 毫秒期限在 421 毫秒释放，两轮相同。Usage 两次均只申请了剩余宽限期内的等待，内核等待在到期后 1.2 / 1.7 毫秒返回，`ESTIMATED` 状态在到期后 3 / 4 毫秒写入账本。 |

每轮有 9 个测试。额外断言覆盖了仅回滚的保存点、只唤醒就绪 Run 的原生管道、文件系统不支持 FIFO 时的跨连接 `data_version` 变化、Job 的启动和提交宽限期，以及开放 Job 的 1 秒复查上限。两轮共 18 份 SQLite 快照分别通过完整性检查，归档事件数与对应数据库一致。证据等级为 `SCRIPTED`，使用本地进程、SQLite、真实 CPU 命令和信号，全程离线。[专项回执](acceptance/closure/idle-history/dc70b1133e4f/receipt.json) · [测试源码](gx-h/tests/test_p2a_idle.py)

## 运行行为

- 两个入口共用工作区等待器。它在检查待办事项前打开命名管道，扫描与进入等待之间到达的消息也可唤醒。SQLite 写入方在最外层提交和事务结束回调之后发出提示。读事务、仅回滚的写入和 Driver 自己的连接都不会产生自唤醒。分离式 Runner 在原子发布状态后发出同类提示，包含写完结果后的终态发布。[等待器](gx-h/gx_core/p0/idle.py) · [事务提交](gx-h/gx_core/persistence/store.py) · [Job Runner](gx-h/gx_core/jobs/runner.py)
- 原生 stdout、stderr 的可读描述符与工作区通知在同一 selector 上等待。原有传输负责实际读写，多个 Run 仍在同一条 Driver 线程上推进。一项任务释放跨 Run 的工作区或提供方配额时，相关调度会重新检查准入。原有传输故障退避与限额仍由各自的业务模块执行。[传输](gx-h/gx_core/hostobs/app_server_client.py) · [共享协调器](gx-h/gx_core/p0/coordinator.py)
- 每次休眠取指数退避和生效期限中的最早值。期限来自 Job、Usage 宽限期、Lease、活跃原生心跳、终态产物、消息或调度重试以及 CLI 总超时。恢复时由原 Job、Lease 和计量服务执行状态转换。Usage 可在离线状态按宽限期结算。[Driver](gx-h/gx_core/p0/driver.py) · [Job](gx-h/gx_core/jobs/service.py) · [计量](gx-h/gx_core/p0/metering.py)
- 同机 GX 生产者可主动通知。跨主机共享盘、绕过 GX 事务接口的写入、通知丢失时，普通空闲复查上限为 4 秒，存在开放 Job 时为 1 秒。若文件系统不能创建 FIFO，等待器每 250 毫秒检查一次 SQLite 版本，保留同样的业务期限。数据库工作、已在执行的同步回调和内核调度时间均可能增加端到端延迟。表中的等待返回与账本提交各有独立时间戳。[等待器](gx-h/gx_core/p0/idle.py) · [Job 期限](gx-h/gx_core/jobs/service.py)

## 复跑与历史

专项使用基础提交 `e9246ac10eab6d2b83ba801285b2f975fb4adda5` 和 12 个候选文件的逐文件 SHA-256。组合指纹为 `dc70b1133e4f77297097116180c1996634713db5076f945030588314bf977efd`。原始包内 `source.json`、`overlay/`、`receipt.json` 可重建测试源码，`first/`、`repeat/` 保存两轮快照和观测，`real-idle/` 保存实时时钟探针与数据。统一入口另保留工作树版本与差异指纹。[专项文件清单](acceptance/closure/idle-history/dc70b1133e4f/raw-files.sha256) · [统一入口环境](acceptance/closure/idle-history/idle-final/environment.json)

相邻的 27 个既有测试在相同确定性 profile、原有模拟原生传输下全部通过。首轮有一次混合工作区恢复在进入等待前被 CPU 准入拒绝，当时另一个 Run 的 Job 占满该库的两个线程。该用例独立复跑通过，整组同配置复跑也通过。另一次诊断显式设置 `GX_CODEX_OFFLINE=1`，导致 6 个依赖模拟原生传输的测试失败。更早的导出遗漏打包的 Cordis 依赖，随后使用发行包自带的离线安装命令恢复。上述输出分别完整保存在 `regression/`、`isolated-mixed/`、`regression-repeat/`、`regression-same-profile/` 和 `previous-export/`。[各次命令及退出状态](acceptance/closure/idle-history/dc70b1133e4f/receipt.json)

早期统一验收还曾用整个暂停快照返回时间判断唤醒，受额外快照读取影响超出测试上限 5.2 毫秒。修正后的测试分别测量计划等待、实际唤醒和持久化结算时间。原失败包和仅重跑失败项的记录保存在 `early-public/`。[归档及校验清单](acceptance/closure/idle-history/dc70b1133e4f/manifest.sha256)

在发行目录可复跑所选用例，并校验已交付的统一证据包。将 `--python` 指向已安装本发行依赖的解释器。

```sh
bin/gx-acceptance run phase2a --case IDLE01 --case IDLE02 --case IDLE03 --python ../gx-h-v-3-2/.venv/bin/python
bin/gx-acceptance verify idle-final --runs-dir acceptance/closure/idle-history
```
