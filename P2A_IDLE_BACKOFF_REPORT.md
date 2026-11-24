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
- 同机 GX 生产者可主动通知。普通 Driver 空闲退避的上限为 4 秒，开放 Job 的复查上限为 1 秒。P2A.1 将原先仅在无法打开 FIFO 时启用的 SQLite 版本复查扩展到所有等待器。每 250 毫秒读取一次连接版本，仍按最近的业务期限返回。数据库工作、已在执行的同步回调和内核调度时间均可能增加端到端延迟。表中的等待返回与账本提交各有独立时间戳。[等待器](gx-h/gx_core/p0/idle.py) · [Job 期限](gx-h/gx_core/jobs/service.py)

## 复跑与历史

专项使用基础提交 `e9246ac10eab6d2b83ba801285b2f975fb4adda5` 和 12 个候选文件的逐文件 SHA-256。组合指纹为 `dc70b1133e4f77297097116180c1996634713db5076f945030588314bf977efd`。原始包内 `source.json`、`overlay/`、`receipt.json` 可重建测试源码，`first/`、`repeat/` 保存两轮快照和观测，`real-idle/` 保存实时时钟探针与数据。统一入口另保留工作树版本与差异指纹。[专项文件清单](acceptance/closure/idle-history/dc70b1133e4f/raw-files.sha256) · [统一入口环境](acceptance/closure/idle-history/idle-final/environment.json)

相邻的 27 个既有测试在相同确定性 profile、原有模拟原生传输下全部通过。首轮有一次混合工作区恢复在进入等待前被 CPU 准入拒绝，当时另一个 Run 的 Job 占满该库的两个线程。该用例独立复跑通过，整组同配置复跑也通过。另一次诊断显式设置 `GX_CODEX_OFFLINE=1`，导致 6 个依赖模拟原生传输的测试失败。更早的导出遗漏打包的 Cordis 依赖，随后使用发行包自带的离线安装命令恢复。上述输出分别完整保存在 `regression/`、`isolated-mixed/`、`regression-repeat/`、`regression-same-profile/` 和 `previous-export/`。[各次命令及退出状态](acceptance/closure/idle-history/dc70b1133e4f/receipt.json)

随后，干净提交 `d5df10e` 的完整套件再次遇到同一 CPU 准入错误。确定性复现让同库另一个 Run 的两个 Job 在实际获得准入后留在 `CREATED`，两者占满线程配额。原实现将可恢复的临时拒绝当作阶段执行失败。修复后，可重入阶段以 `WAITING_EXTERNAL_JOB` 保存检查点，冷启动通过 Job 服务的 `cpu_room` 检查整个工作区的实际可用线程。释放一个槽位只准入一个操作，随后再次等待。第二个槽位释放后，重试复用第一个 Job，两个原定操作均成功完成。需要的线程数超过总配额时，保留原始拒绝。[首轮集成记录](acceptance/closure/runs/full-d5df10e-20261124T1533Z/summary.json) · [检查点与准入实现](gx-h/gx_core/orchestration/engine.py) · [等待状态投影](gx-h/gx_core/orchestration/reactor.py)

固定占用的复现用例在原版本失败。修复后的两项定向测试连续两次通过，47 项相邻回归全部通过，覆盖原有混合工作区恢复、阶段隔离、空闲唤醒和实际分离式 Job。修复前后的数据库和事件记录均已归档，4 份快照通过 SQLite 完整性检查。[修复验证回执](acceptance/closure/idle-history/cpu-admission/receipt.json) · [原始数据库与日志](acceptance/closure/idle-history/cpu-admission/raw.tar.gz)

早期统一验收还曾用整个暂停快照返回时间判断唤醒，受额外快照读取影响超出测试上限 5.2 毫秒。修正后的测试分别测量计划等待、实际唤醒和持久化结算时间。原失败包和仅重跑失败项的记录保存在 `early-public/`。[归档及校验清单](acceptance/closure/idle-history/dc70b1133e4f/manifest.sha256)

在发行目录可复跑所选用例，并校验已交付的统一证据包。将 `--python` 指向已安装本发行依赖的解释器。

```sh
bin/gx-acceptance run phase2a --case IDLE01 --case IDLE02 --case IDLE03 --python ../gx-h-v-3-2/.venv/bin/python
bin/gx-acceptance verify idle-final --runs-dir acceptance/closure/idle-history
```

## P2A.1 工作区 FIFO 交接

公共 `run` 和 `resume` 在 `.native.lock` 的独占锁内打开工作区等待器。直接调用 `Driver.drive` 或 `WorkspaceWait` 也能打开读取端，另一个进程可在前一读取端关闭前持有相同 inode。现在每个等待器仅关闭自己的描述符，FIFO 名称跨正常退出和异常退出保留。读写双方均要求实际节点为当前有效 UID 持有、模式为 `0600` 的 FIFO，并在打开后核对类型、权限和 inode。普通文件、符号链接、其他属主或错误权限的节点留给其所有者处理。生产者在事务外发通知，待办提交和分离式 Job 的状态发布沿用原有接口。[工作区锁](gx-h/gx_core/native_release.py) · [CLI](gx-h/gx_core/p0/cli.py) · [读写端校验](gx-h/gx_core/jobs/wakeup.py) · [等待器](gx-h/gx_core/p0/idle.py)

两次确定性离线验收各通过 5 项。旧版在交接时删除了后继读取端使用的名称，还复现了两读取者竞争一个字节时的 `EAGAIN`。修复版的第二个进程先确认原生锁被占用，随后接管原锁及同一 FIFO。进入等待前发送的纯 Job 提示两次均可读取，在已阻塞时发出的提示分别在 0.215 和 0.271 毫秒后唤醒。替换 FIFO 后，两个仍持有旧描述符的进程在 SQLite 提交后约 149 毫秒返回。单读取端对照中，旧版耗尽 1 秒等待，修复版在提交后 202 毫秒观察到变化。两份 SQLite 备份均通过完整性检查，行内容逐项匹配原始记录。[专项回执](acceptance/p2a1/fifo/result.json) · [测试源码](gx-h/tests/test_p2a1_fifo.py)

| 同一 IDLE01 Run 的真实空闲窗口 | 墙钟秒 | 进程 CPU 秒 | 实际阻塞秒 | 等待次数 | SQLite 版本读取次数，含首次 | 每秒读取 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `61ea96d` | 5.012 | 0.063 | 4.948 | 6 | 1 | 0.20 |
| 修复首轮 | 5.025 | 0.126 | 4.679 | 6 | 20 | 3.98 |
| 同版复跑 | 5.029 | 0.091 | 4.951 | 6 | 20 | 3.98 |

SQLite trace 回调记录每次版本读取的单调时钟。健康 FIFO 上的额外复查维持约每秒 4 次，两轮 Driver 均只进入 6 次阻塞等待。版本变化是复查提示，事务回调、Job 的持久化文件及其原有复查期限仍由各自模块处理。22 项既有空闲、CLI 恢复和 CPU Job 回归全部通过。[原始时序、负例及快照](acceptance/p2a1/fifo/raw.tar.gz) · [校验清单](acceptance/p2a1/fifo/manifest.sha256)

在 Linux 上关闭真实描述符后，修复前的等待器在 epoll 注册和退出清理时均抛出 `EBADF`。冻结提交 `b1a0ad0` 还覆盖了 FIFO 就绪后、读取前描述符失效的情况。两种故障下等待器都继续运行，原生管道的就绪事件仍送达，每次故障后的数据库提交也都被观察到。该提交的独立导出连续两轮各通过 5 项，22 项相邻回归全部通过。两轮空闲各阻塞 6 次，累计均为 4.86 秒，进程 CPU 分别为 0.112 和 0.137 秒。含初始化在内，两轮各读取 20 次版本，实测每秒 3.84 和 3.70 次。[冻结版本回执](acceptance/p2a1/fifo/descriptor-errors/result.json) · [内核错误、原始时序及数据库](acceptance/p2a1/fifo/descriptor-errors/raw.tar.gz)

完整 Python 回归在 `5bc7c7e` 的 482 项测试中记录到 1 个夹具错误。验收命名空间只映射 UID 0，内核以 `EINVAL` 拒绝将 FIFO 属主改为 65534。`c9fc23f` 在宿主上仍以 UID 65534 真实持有的 FIFO 验证拒绝，在单 UID 命名空间里仅对目标 FIFO 的 `lstat` 属主读数使用夹具，并保存 UID 映射及内核错误。该提交的干净导出在两种环境各通过 5 项。独立副本取掉属主比较后，两边的负例都在读取端错误打开 FIFO 时失败。[宿主和命名空间对照](acceptance/p2a1/fifo/user-namespace/result.json) · [原回归日志、属主记录及反向验证](acceptance/p2a1/fifo/user-namespace/raw.tar.gz)

在发行目录可独立执行 `bin/gx-acceptance run hardening --case FIFO`。专项也可在 `gx-h` 下直接运行 `python -B -m unittest -v tests.test_p2a1_fifo`，设置 `GX_FIFO_EVIDENCE` 为新目录即可保存同样的时序与快照。
