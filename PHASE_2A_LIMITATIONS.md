# Phase 2A 验收范围

- **科研能力。** 本阶段验证静态 Workflow 的持久执行。独立三分支夹具使用真实本地 CPU Job、Artifact 和预算所有者；算法和预定的决策等待是脚本输入。动态 GraphVersion、Amendment、完整 Decision Lease、独立插件和自主选择真实科学方法按后续阶段验收。[Phase 1.5 阶段边界](V32_PHASE_1_5_DELTA.md) · [夹具定义](acceptance/phase2a/static_case.py)
- **Codex 边界。** 官方二进制与本地脚本化 Responses 服务可验证 App Server 协议、回执和进程中断。停止范围是该测试独占环境里的 Codex/App Server。计量只接受原回执能证明的用量；用户要求的正式模型决策仍需提供者可用。本轮没有调用付费模型、远程调度器或第二 Provider。[进程证据入口](acceptance/phase2a/cross_process.py)
- **持久化边界。** 当前使用单个 GX SQLite 项目和既有 detached Job 文件。运行身份、产物和结算的去重结果对应已测故障窗口。数据库、文件系统和 Job 后端同时丢失，以及远端调度器已收件但无法按操作身份查询，均需相应后端另行验证。已有多等待点的项目应由支持该格式的 GX 打开。[本地 Job 执行器](gx-h/gx_core/jobs/executor.py) · [回退演练](PHASE_2A_ROLLBACK_REPORT.md)
- **测量口径。** 性能样本来自指定静态图、短时空闲窗口及本机临时状态目录。样本的 CPU 时间、唤醒、SQLite writer 耗时和零重复计数均按实际观测报告。长时间调度、共享存储负载、GPU 性能与付费模型延迟需要分别测量。[统计程序](acceptance/phase2a/performance.py)

## 额外计量故障

在独立的旧探针中，运行时丢失 child spawn 通知，冷恢复又丢失一次启动通知，基线与最终候选均触发对账超时。父线程的 3 笔回执各 35 token，与累计 105 一致。子线程累计 915 token，GX 持有 4 笔各 183 的回执，共 732。缺口 183 token 对应哪笔调用尚未由回执证明。GX 保留原 `HELD` 预留，`settled_tokens` 为空。此组合仍待解决，未计为 P2A-11 已验证的子线程自动结算。正式 P2A-11 的两项独立运行和 13 项原生专项门禁另有记录。[基线原始行](acceptance/phase2a/regression/baseline-combined-loss/observation.json) · [最终候选原始行](acceptance/phase2a/regression/final-v2/batch/extra-child/observation.json) · [正式 child 门禁](acceptance/phase2a/regression/final-v2/batch/child-lost-start.json)

旧版终态协议探针在看到 `turn/completed` 后立即读取账本。基线第 45 项、候选第 23 和第 68 项都记录过中断已完成、一笔 35-token response 已登记而官方累计尚缺、预留仍为 HELD。这三次旧进程退出后的最终用量无可核对的记录。新版探针保留核对原 turn、正式确认、精确结算和零额外模型请求的断言，对迟到回执给出有界等待，另用同一个真实官方帧验证迟到次序。覆盖的是回执可取得的情形。[原基线与候选探针复测](acceptance/phase2a/regression/final-v2/terminal-protocol-controls) · [当前探针](acceptance/v31/terminal_protocol_probe.py) · [公开冷恢复的精确结算](acceptance/phase2a/regression/final-v2/batch/terminal-public-crash.json)

进一步的官方宿主实验区分了通知交付丢失与累计凭据不可取得。前者可在核对原线程、原 turn 和已关闭的 Goal 后，通过新宿主重放并完成一次精确结算；后者在一次自然中断的受测窗口内，经 10 秒原宿主轮询及 5 秒新宿主恢复，仍只有 35 token 的已观测响应下界。最后一次直接读取账本发生在冷恢复之前，预留为 HELD；新宿主没有收到可归属的累计，也没有发起模型请求。失败路径退出前没有再次读取账本。原协议探针直接调用 Roots，这个现象同时出现在旧基线上。Phase 2A 的静态 Engine/Driver 能继续独立工作，无法从现有正式回执补出缺失的归属与完整性。因此 P2A-09 维持 PARTIAL。[故障对照与判定](acceptance/phase2a/regression/final-v2/adjudication.json) · [锁定的 Codex 来源与版本](release-assets/lock.json)
