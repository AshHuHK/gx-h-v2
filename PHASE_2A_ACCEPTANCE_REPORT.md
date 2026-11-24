# Phase 2A 签收记录

**阶段结论为 PARTIAL。** 15 项矩阵中，P2A-01 至 08 和 P2A-10 至 15 共 14 项通过。P2A-09 的终态提交、取消、去重及有官方用量回执的冷结算通过；部分自然中断缺少可归属的累计凭据。合同要求完整的 v3.1 回归通过，因此本轮可签收静态分支执行与恢复，仍有一项待解决。

本轮以 `51e0e13583d95e9cad1386e1d4c77324977e97ea` 为基线。Phase 1.5 在实现前对齐六份 ADR，11 条决策均有后续阶段归属。[决策清单](V32_PHASE_1_5_DELTA.md) · [原始实施包](GX_H_Phase1_5_Phase2A_Package.zip)

## 故障窗口

第一组 Run 先建立 A 的决策等待和 B 的 Job 等待，再杀掉该测试命名空间中实际运行的官方 App Server。在同一个 GX PID 内，Driver 观察原 Job 的终态，B2 发布汇总 Artifact，B3 重读并检查数值，A 的原等待继续有效。独立分支 C 的输出记录了执行时看到的节点状态。

第二组 Run 先停止 Codex，再让 C1 完成，在两支仍等待时向 GX 发送 SIGKILL。新的 GX 进程打开同一数据库文件，由原 Job ID、runner PID/启动时间及原始结果摘要认领产物。重启前已经完成的节点均只出现一次；对账缺少官方回执的原生用量仍保持 HELD。空闲阶段单独计时。

产品冻结为 `c15eada9611f60519bafbb3366c908629dbea157`。稳态 Run 为 `gxr_01m85gwmfpsw0ab16t`，重启 Run 为 `gxr_01m85gwyzar1xhp74x`。二者各自使用原 CPU Job，停机后 Codex 新启动次数为 0；重复节点完成、重复 Artifact 和重复结算均为 0。官方二进制为 Codex CLI 0.169.0，Responses 由本地测试服务生成。全部回合均未访问付费模型。[运行回执](acceptance/phase2a/cross-process/observations.json) · [隔离网络与二进制](acceptance/phase2a/cross-process/network.json) · [原始进程、数据库与事件](acceptance/phase2a/cross-process/restart)

## 逐项结果

| 项目 | 可复核的现象 | 原始证据 |
|---|---|---|
| P2A-01 · 同 Run 的 A 与 B | A1 的 planner 等待保留，原 Job 产生可核对的原始数据，B2 汇总、B3 数值检查完成。 | [稳态运行](acceptance/phase2a/cross-process/steady) |
| P2A-02 · 第三支 C | C1 的输出保存执行时看到的节点状态，A1 正待决策、B1 正等 Job。 | [重启组的运行与输出](acceptance/phase2a/cross-process/restart) |
| P2A-03 · 停止 Codex | 向测试命名空间中实际运行的 App Server 发 SIGKILL；GX 在无宿主窗口继续执行并留下新的完成事件和产物。 | [进程观测](acceptance/phase2a/cross-process/observations.json) |
| P2A-04 · 杀掉 GX | 原 GX 进程 103791 被杀；104370 打开原数据库和原 Job 侧车后恢复。 | [命令、PID 和退出码](acceptance/phase2a/cross-process/restart/commands.json) |
| P2A-05 · 对账与计算并存 | 原生预留 128 token 和 1 次调用仍为 HELD；原 Job 在预留未结时完成，独立节点及其产物均已登记。 | [等待与账本](acceptance/phase2a/cross-process/observations.json) |
| P2A-06 · 已完成节点 | 每个 Run 原定五个可完成节点各完成一次，一份原始观测和一次 Job 结算，重启后状态与事件数一致。 | [事件流](acceptance/phase2a/cross-process/restart/events.jsonl) |
| P2A-07 · 原等待 | 冷恢复保留 A1 的 owner、原引用、任务、原因和起始时间；A2 未越过依赖。 | [重启前后 SQLite 快照](acceptance/phase2a/cross-process/restart) |
| P2A-08 · 独立分支失败 | 失败节点的事务副作用回滚，C 成功一次；授权重试后其子节点才运行，另一个待审批引用始终相同。 | [失败及重试回执](acceptance/phase2a/branch-failure/evidence.json) |
| P2A-09 · 终态提交、ACK、恢复 | **PARTIAL**。提交与取消均已记录，正常冷恢复精确结算。204 回合重复试验发现官方累计回执偶发缺失；有实际回执的成功批次与无回执的失败批次分别保留。 | [正式协议与故障回执](acceptance/phase2a/regression/final-v2/batch) |
| P2A-10 · 同 Attempt 修复 | 经公开入口拒绝错误 schema，原 Attempt 可修复提交；32 路合同在真实官方宿主上分页重组，与原 schema 的摘要一致。 | [公开 repair](acceptance/phase2a/regression/final-v2/batch/public-repair.json) · [线路回执](acceptance/phase2a/regression/final-v2/batch/schema-transport.json) |
| P2A-11 · 原生 child | 核对 8 份角色工具目录，以及正常、丢失冷启动通知两种运行各 19 项检查；范围外 Artifact、普通 fork 和过期租约均被拒绝。组合丢失边界见限制说明。 | [正常路径](acceptance/phase2a/regression/final-v2/batch/child-scope.json) · [丢失通知](acceptance/phase2a/regression/final-v2/batch/child-lost-start.json) |
| P2A-12 · Brief | 对保留的 13 组历史输入重算 26 个 Run、78 个分配的 Brief，并用官方宿主验证线路及新增资料的新鲜度。 | [历史重算](acceptance/phase2a/regression/final-v2/batch/brief-replay.json) · [线路实测](acceptance/phase2a/regression/final-v2/batch/brief-wire.json) |
| P2A-13 · 版本兼容 | 公开 science_grid@3 的确定性和 reviewer 路径均跑通；历史 @2 的 114 张业务表经两次可写打开核对，独立冷 checkpoint 继续执行到终态。 | [确定性](acceptance/phase2a/regression/final-v2/batch/public-deterministic.json) · [reviewer](acceptance/phase2a/regression/final-v2/batch/public-review.json) · [历史表](acceptance/phase2a/regression/final-v2/batch/at2-preservation.json) · [冷恢复](acceptance/phase2a/regression/final-v2/batch/at2-cold.json) |
| P2A-14 · 空闲 Driver | 5.021 秒墙钟内消耗 0.181 CPU 秒，约单核 3.60%；9 次真实睡眠返回，约 1.79 次/秒。 | [原始计时和汇总](acceptance/phase2a/performance/summary.json) |
| P2A-15 · Job 后端暂时不可读 | 预留延后期间，同一轮对账处理了另一项 Job 的实际非零退出；原 Job 后续成功。各启动、结算、通知一次。 | [两项 Job 和受控故障](acceptance/phase2a/jobs/result.json) |

## 无 Codex 窗口的性能

同一组脚本化输入使用真实 GX、SQLite 和 detached Job。下表为扣除停机前样本后得到的最近秩分位数，单位毫秒。Driver 周期与空闲睡眠分别测量。23 次 Job 对账中有 3 次实际改变状态。13 次节点扫描分别观察到 0–3 个就绪节点和 0–2 个等待节点。此处没有预设性能阈值。[94 条原始样本](acceptance/phase2a/performance/owner-timings.jsonl.gz) · [统计口径与各分布](acceptance/phase2a/performance/summary.json)

| 操作 | 样本 | p50 | p95 |
|---|---:|---:|---:|
| 就绪节点扫描 | 13 | 0.017 | 0.053 |
| Driver 周期 | 23 | 16.084 | 45.073 |
| 已有 Job 对账 | 23 | 0.126 | 8.006 |
| SQLite 最外层写事务总时长 | 35 | 0.802 | 43.481 |
| 其中等待取得写锁 | 35 | 0.006 | 0.034 |

独立公开 CLI 演练还验证了新建、停止、恢复和只读状态查询。停止后的 Run 为 PAUSED；恢复后回到同一 reviewer 的等待点，原来两项 Job 都保持 SUCCEEDED。历史回退另附 7 次公开 CLI 调用和数据库摘要。[CLI 回执](acceptance/phase2a/public-cli/receipt.json) · [回退报告](PHASE_2A_ROLLBACK_REPORT.md)

## v3.1 回归及修复经过

- 第一个候选 `2a9ec17` 的同 Run 断电验收通过，冷终态恢复有两条实际回归。Driver 将关闭 Goal 前的一次读取缓存为已经完成，终态 owner 的处理也未在原时机执行。修复后，`c15eada` 在官方宿主上先确认原 Goal 已关闭，再以禁用 Goal 的方式读取同一 turn 的累计回执。原 105 token 精确结算一次，恢复期间新增模型请求为 0，最终项目状态为 COMPLETED。[第一次失败批次](acceptance/phase2a/regression/final/summary.json) · [最终官方冷恢复](acceptance/phase2a/regression/final-v2/batch/terminal-public-crash.json)
- 原 schema 探针在基线和候选都把提交工具当成顶层工具。夹具修正为从真实的 `gx` 命名空间读取后，两版官方宿主均在 8 页重组出相同的 19,259 字节。32 路的精确 schema、缺少一路时拒绝和传输字节的原断言全部保留。[基线与候选的独立对照](acceptance/phase2a/schema-transport/evidence.json)
- 同一最终产品上的 Python 集合为 465 项，其中 461 项通过，4 项因可选 DeepSeek SDK 环境缺失而沿用基线跳过。P0 专项 127/127、Node 2/2、生成器、13 项原生专项、17 项既有官方宿主场景通过。P0 与 Python 集合有重叠。五次 code-mode 和五次 stream 各保留原始线路回执。[完整测试统计](acceptance/phase2a/regression/final-v2/batch/suite/summary.json) · [测试身份与跳过项](acceptance/phase2a/regression/final-v2/batch/v31-test-identity.json) · [code-mode](acceptance/phase2a/regression/final-v2/batch/terminal-code-mode.json) · [stream](acceptance/phase2a/regression/final-v2/batch/terminal-stream.json)
- 在未改的 204 回合终态协议探针中，第一次冻结版本有完整成功批次。最终产品的完整回归批次有 19/20 个操作退出 0，唯一失败操作是协议探针。旧基线和最终版都出现过能读到中断终态和一笔 35-token 响应、却尚不能精确结算的 turn。在另一个真实中断中，等候 10 秒、正常关闭原宿主，随后在新官方宿主上核对原绑定和已关闭的 Goal、禁用 Goal 并等候 5 秒，仍未得到该 turn 的累计。冷恢复前最后一次直接读取的预留为 HELD；新宿主未收到累计且没有新模型请求。该失败路径退出前没有再次读取账本。对照组刻意遗失了一个实际发出的通知，新宿主能重放原累计并只结算一次。两者分别计入验收。历史三次失败进程退出后的账本状态仍为未知。[初次候选](acceptance/phase2a/regression/final/summary.json) · [最终完整批次](acceptance/phase2a/regression/final-v2/summary.json) · [有界暖恢复对照](acceptance/phase2a/regression/final-v2/terminal-protocol-bounded) · [最终签收索引](acceptance/phase2a/regression/final-v2/adjudication.json)

P2A-09 的剩余条件是获得能归属于原 turn 且证明覆盖完整的官方凭据。GX 当前有可追溯的响应用量下界，保留原预留供后续对账。额外的 child 组合丢帧也有独立的未结记录；两个缺口均无估算入账。[能力限制与账本明细](PHASE_2A_LIMITATIONS.md)
