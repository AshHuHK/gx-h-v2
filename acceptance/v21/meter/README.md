# v2.1 原生用量与预算验收

基线为 `2314eed7`。在真实临时 SQLite 上运行原版 Root 投递，缺失最终回执的 turn 在逻辑时钟推进 86,401 秒后仍占用 100 tokens 和一次 GX call。另一场把预算设为 1,000,000，已结算 850,000，两个在途 Root 各自报告 100,000 与 80,000，原版仍向第三个 Root 发出 18,928-token 预留，未对前两个 turn 发出中断。这里使用可控的协议消息。[基线记录](baseline.json)

## 对账

一个 GX token unit 对应官方 `totalTokens` 一个单位，cached input 已计入该字段。报表分别列出 GX call 次数、token units 和可用的供应商金额数据。本次没有供应商价格或货币账单。父线程和通过原生关系核实的子线程各有累计主账，同一子线程跨多个 turn 只累计一次。子线程产生的用量由发起它的 GX 预留结算。[累计账实现](../../../gx-h/gx_core/p0/metering.py)

| 验收项 | 运行结果 | 可核查的证据 |
|---|---|---|
| A01、A02 | 35 个总 tokens 中包含 25 个 cached input。重复通知仍收 35，后到的下降值 31 留下异常记录 | [测试日志](tests.log) |
| A03、A05 | 在默认 30 秒 grace 后，终态 turn 以已知下界和原预留中的较大值结算，原始收据标为 ESTIMATED。没有对应终态的模糊发送继续等待原 ACK | [单元测试](../../../gx-h/tests/test_v21_metering.py) |
| A04、A06 | 两条 SQLite 连接重复投递 70 和 130。初估 100 的记录保持原值，账本各追加 −30 和 +60 一次，净额为 130，GX calls 为 1 | [测试日志](tests.log) |
| A10 | 官方同一线程的首次估计为 100，后续累计为 70。热连接第二 turn 有 35 的独立响应，冷连接第二 turn 仅有公开累计。两路均以两次 GX calls 结清 70，无法定位的分摊保留为 UNATTRIBUTED_THREAD_USAGE | [热连接](../protocol/cumulative-warm.json) · [冷连接](../protocol/cumulative-cold.json) |
| A05、A10 反例 | 冷连接后续的两个相同 70 仍缺最终数据，在 grace 后形成第二笔估计。第三个 turn 的新累计 105 足以校正线程，重放保持 105 和三次 GX calls | [重复累计与调整](../protocol/cumulative-incomplete.json) |
| 原生 child | 父线程先终态，父 70 加 child 183 最终按一个 GX call 结算 253。另一次 child 末响应缺失时，在 grace 后按 100 估计并保存下界 70 | [完整 child](../protocol/child-no-rollout.json) · [缺失 child](../protocol/child-missing.json) |
| A07、A08 | 官方两个独立工作线程报告 30 和 73，硬限 100。两个原 turn 收到 `turn/interrupt` 并终态 `interrupted`，超限的完整 103 如数计量，下一次付费入场被拒 | [官方协议与真实 GX 预算](../protocol/budget.json) |
| 软预算与 Job | 在软阈值内提交的 CPU Job 成功，重放返回原 Job。达到阈值后新增低优先级提案保留为 PROPOSED，Job 数仍为 1。测试中提高阈值后同一提案执行成功，最终只有两个 Job | [真实 CPU Job 与幂等回执](soft-jobs.json) |

官方协议场景使用锁定 Codex 0.169.0 和可控 loopback 模型。GX 对原生 rollout 的 Python 文件访问在无文件场景中被禁用。新项目默认只依赖公开 App Server 事件和 GX 自己的 SQLite 收据。升级时已有付费历史的 run 保留旧诊断选项，可用 `p0_spec.metering.rollout_diagnostics=false` 关闭。[协议验收](../protocol/README.md)

## 阈值与暴露

`p0_spec.metering.grace_seconds` 配置终态 grace。可选的 `soft_tokens` 达到后，带 `task.input.priority="low"` 的任务暂缓新的 turn 和 Job。已提交的 Job 仍可按原幂等键查询。硬限复用团队、run、workspace 或项目的已授权预算线。实际观测超过预留的部分立即减少剩余可准入量，已估计结算也占用预算。[硬限与软限](../../../gx-h/gx_core/p0/meter_limits.py)

状态报告给出已结算量、在途已观测量、当前最大单 turn 预留、活跃 Root 数和预留剩余暴露量。设最大单 turn 预留为 (G)，并行 Root 数为 (N)，GX 在该配置下可分配的同时预留至多为 (N G)，还会受到实际共享余额约束。如供应商能独立保证每个 Root 尚未上报的请求上限为 (U)、每秒新增用量上限为 (R)、从观测至中断生效的延迟上限为 (L)，则该窗口附加暴露至多为 (N(U+RL))。这里的官方接口未给出这些保证，`provider_maximum_overshoot_tokens` 如实返回 null。官方 103/100 的记录直接量出了此次超额 3。[风险记录](../protocol/budget.json)

## 复验

从 v2.1 仓库根目录运行以下命令。测试覆盖有名终态、迟到累计、缺失用量、旧库冷升级、重复通知、两连接竞争、原生 child、软硬阈值和原有付费/租约回归。[运行日志](tests.log) · [退出码](tests.exit)

```bash
PYTHONPATH=gx-h:gx-h/tests .venv/bin/python -m unittest -v tests.test_v21_metering tests.test_p0_root_delivery tests.test_p0_meter_recovery tests.test_p0_wait_leases tests.test_p0_roots
```
