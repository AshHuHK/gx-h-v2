# P2A Closure 源码审计与实现决策

分支 v3.2-p2，审计 HEAD 为 78a8b07766376d93a458e5175c41f1f327ff8f36。执行了 git status --short、git branch --show-current、git rev-parse HEAD 及 git log --oneline -20。开始时只有新提供的 GX_H_P2A_Closure_Package.zip 未跟踪，SHA-256 为 afca692f1d4c379601912b5f0ffe2be8244cb7b92c0a54b65f0a41271d7ff302。上次产品冻结于 c15eada9611f60519bafbb3366c908629dbea157。下列行号指向审计 HEAD。

| 状态 | 入口和事务边界 | 实际行为与应补的验收 |
|---|---|---|
| existing / partial | gx-h/gx_core/p0/root_delivery.py:389–435,519–596。admission 事务中保存预留与基线，ack 绑定 Turn，observe 事务从 :729 起消费通知 | 正常完整 Turn 可以精确结算。:200–236 的 (thread,response) 明细与 :770–790,843–871 的 child 验证分别有证据。终态后的晚到明细缺乏统一触发。需要同一预留下精确结算或追加调整，保留原事件。 |
| bug | gx-h/gx_core/p0/metering.py:132–217,248–335 观察 Thread 前缀，按同一资金来源调整。预算 owner 的 settle/adjust 由外层事务包住 | Thread 累计含同 Thread 先前响应。prefix 有未归属增量时，不能换算某个 Turn 的实际成本。旧 estimate 使用 grant/lower，证据不足也可能被结算。须分别展示有归属的 receipt、保守估算、未决和未归属差额。 |
| existing / partial | 锁定版 Codex Rust protocol/src/protocol.rs:2593–2629，core/src/state/session.rs:253–288，core/src/session/mod.rs:5678–5718，app-server/src/bespoke_event_handling.rs:1245–1255,1653–1668。路径相对 codex-rs/ | 读到的 response 事件与 thread 快照有不同归属和发出时机。中断时即使已有 raw 明细，仍可能缺闭合证据。见 acceptance/phase2a/regression/final-v2/adjudication.json。旧独立 child 样本含 parent 105，child 累计 915，有归属的 child receipts 732，剩余 183 的归属未知。 |
| partial | gx-h/gx_core/p0/driver.py:564–611。先处理本地投递、Jobs 和 Reactor，随后 metering.maintain，再读取 native 状态 | 保留这个维护 owner。断线、停止与重启跨 grace 的路径须落真实 DB 与事件，对估算后的重复或晚到回执做业务幂等验证。 |
| partial / missing | gx-h/gx_core/jobs/capacity.py:62–90,230–254,276–293；jobs/service.py:698–705,734–746,940；hostobs/detect.py:47–86；hostobs/caller.py:53–55 | Job 准入在原事务中消费实际容量。2 核按默认 0.5 会得 1 Job thread。检测读取环境中 Claude 标记。regression/run.py:64–79,94–103 和 acceptance/v21/regression.py:70–83,117–127 的过滤范围不同。需固定测试 profile、明确子进程环境，另测真实资源。 |
| missing evidence | acceptance/phase2a/regression/final-v2/batch/suite/python-full.log 及用户 ZIP 六个文件 | 可识别旧 465 IDs 和该次四个可选 SDK 跳过。包只给出另一次的失败总数 42，未列 IDs 或 traceback。先查独立日志，再逐项对照；禁止按数量猜测身份。 |
| partial | gx-h/gx_core/persistence/store.py:502–565 的 outer BEGIN IMMEDIATE、内层 SAVEPOINT、commit/rollback hooks；metrics.py:18–54；acceptance/phase2a/performance.py:16–61 | 已有外层总耗时及获取耗时，未分 body、commit、结果、行数和 caller。入口来自 Engine:874,924,1096,1465,1521,1531,1544；orchestration/reactor.py:222,406,409,507；p0/driver.py:123,384,456,463；metering.py:281；root_delivery.py:467,729,843,995,1072；jobs/service.py:677,1005,1562,1800。使用显式标签，释放写锁后写 metrics。 |
| partial / missing | acceptance/phase2a/regression/run.py:38–105,121–216 创建 frozen worktree、校验依赖、运行分组和写日志。cross_process.py、branch_failure.py、job_unavailable.py 另有专门入口 | 独立目录已有历史证据，缺少覆盖各项的一条命令、全量 hash 归档、失败项续跑和可核验的分类。保留原命令、退出码以及原失败；每个新 run 用新 ID。 |
| partial / bug | gx-h/gx_core/p0/driver.py:511–543,593–626,681–710；p0/coordinator.py:137–216 | 单 Run 用 sleep、工作区每至多 250ms 检查。共享热宿主及终态 HELD 有固定短轮询。新提交需要能唤醒等待，睡眠要受 Job/Usage/Lease 和运行总时限约束。保留单一共享主循环与原 ready-node 算法。 |

## 实施边界

预算控制继续使用原 reservation 和账本事务。成本归属需要相应 Thread/Turn 的证据；hook/冷恢复/维护必须收敛到同一事件。确定性测试使用独立子进程，生产默认使用真实资源。事务指标在同一 writer 边界采样。验收报告保留官方宿主加脚本上游与真实模型的证据区别；已有失败保留原判，新的会计规则单独判定。此批只处理 ZIP 的 U、T、DB、AC、IDLE 与原 2A 回归。
