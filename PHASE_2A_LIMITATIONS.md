# Phase 2A 验收范围

- **科研能力。** 本阶段验证静态 Workflow 的持久执行。独立三分支夹具使用真实本地 CPU Job、Artifact 和预算所有者；算法和预定的决策等待是脚本输入。动态 GraphVersion、Amendment、完整 Decision Lease、独立插件和自主选择真实科学方法按后续阶段验收。[Phase 1.5 阶段边界](V32_PHASE_1_5_DELTA.md) · [夹具定义](acceptance/phase2a/static_case.py)
- **Codex 边界。** 官方二进制与本地脚本化 Responses 服务可验证 App Server 协议、回执和进程中断。停止范围是该测试独占环境里的 Codex/App Server。计量只接受原回执能证明的用量；用户要求的正式模型决策仍需提供者可用。本轮没有调用付费模型、远程调度器或第二 Provider。[进程证据入口](acceptance/phase2a/cross_process.py)
- **持久化边界。** 当前使用单个 GX SQLite 项目和既有 detached Job 文件。运行身份、产物和结算的去重结果对应已测故障窗口。数据库、文件系统和 Job 后端同时丢失，以及远端调度器已收件但无法按操作身份查询，均需相应后端另行验证。已有多等待点的项目应由支持该格式的 GX 打开。[本地 Job 执行器](gx-h/gx_core/jobs/executor.py) · [回退演练](PHASE_2A_ROLLBACK_REPORT.md)
- **测量口径。** 性能样本来自指定静态图、短时空闲窗口及本机临时状态目录。样本的 CPU 时间、唤醒、SQLite writer 耗时和零重复计数均按实际观测报告。长时间调度、共享存储负载、GPU 性能与付费模型延迟需要分别测量。[统计程序](acceptance/phase2a/performance.py)
