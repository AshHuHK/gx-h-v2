# GX-H v3.1 的源码与对照

本次开发从 GitHub 的 `v3` 分支 `10ed4155099cf05fd81de29f739bf481cc00a045` 创建独立分支 `v3.1`。本地旧版工作树当时干净，远端引用与本地一致。修订包 SHA-256 为 `d4c7782e9a833ed9bc8ffa08ec7ff796b67c71842b4e5eb41ecb11cc417bcbc3`。[原始实施包](GX_H_V3_1_Revised_Implementation_Package.zip)

## 固定运行时与调用链

| 所有者 | 在基线源码中观察到的行为 | 对本次修改的影响 |
|---|---|---|
| Codex App Server 0.169.0 | `bespoke_event_handling.rs` 在 `TurnAborted` 先取消该线程的挂起服务端请求，再回答 interrupt。`dynamic_tools.rs` 遇到 turn transition error 直接返回。core 的 `tools/handlers/dynamic.rs` 取消等待，生成取消记录。 | GX 提交事务和回执先持久化；回调返回哨兵，中断由原 turn 承接。GX 的正常工具回复会破坏这一协议。 |
| GX App Server 客户端 | `send` 返回本地请求号，`request` 需读取回执。`_deliver_callback` 原本会为所有有 id 的服务端调用写回复。回调内禁止嵌套读取。 | 终止证明用原 turn 的官方终态和实际模型请求数。已入队或中断命令已写入都不足以签收。 |
| GX Root 与科研提交 | `roots._call` 在同一 SQLite 事务验证 thread、turn、lease 和重放摘要，调用 `TeamService.submit_workflow_product` 后写 `p0_root_calls`。原 `_submit` 只在提交后排队设置 goal。 | 成功的科研结果可随原回执恢复。发生结构错误时先回滚，原回执表不会留下错误次数。 |
| 工作流与科研评估 | 原 `science_grid_v2.nodes.validate` 检查逐路径覆盖、证据、评价及审阅者给出的 supported。`plugins.runtime.for_saved` 与工作流摘要决定恢复时加载的代码。 | 将结构、路径来源、确定性计算和需解释的科学问题分开处理。为新行为选择 `science_grid@3`，历史项目沿用其固定版本。 |
| GX 计量 | `RootDelivery.observe`、`observe_descendant`、`metering.response` 接收响应与用量。`p0_meter_responses` 原有列保留响应身份、turn、总量及冲突标记。原结算还会核对父子关系、终态和累计数。 | 逐响应字段用于归因；结算仍由既有账本负责。来源缺失的字段保留空值。 |
| 派发上下文 | `Roots._new_row` 使用已领任务构造并保存 Root 工作简报，`_settings` 和 `dispatch` 均会使用它。`gx_task_context` 按已固定插件构造任务数据并授权其引用，`gx_read_artifact` 核对当前租约。 | 简报预载在此入口处理，保留来源摘要和延迟读取。角色删减以实际注册工具和岗位职责为准。 |

源码归档对应 Codex commit `d32063f96c2dff1dc96af839b3952abb6acfeda4`。本机逐字节核对了涉及取消机制的五个 Rust 文件、源码包、实验 Schema 包、Linux 发行包和实际 ELF。运行该 ELF 得到 `codex-cli 0.169.0`，文件 SHA-256 为 `b452ca20f838a8855a88a41050ee4a694033fff4ea3ecdad5633e6e0e8d13f2b`。[锁文件](release-assets/lock.json) · [散列核验与对照计数](acceptance/v31/source-baseline.json)

## 已有 13 波实测

| 指标 | v2.1 原生路径 | 本次直接对照 v3 |
|---|---:|---:|
| 完成项目 / CPU Job | 26 / 52 | 26 / 52 |
| 官方模型响应 / 已结算 turn | 369 / 78 | 364 / 78 |
| 按 provider 回执结算的 tokens | 8,647,052 | 8,638,327 |
| 首次 reviewer 提交被拒 | 26 / 26 | 26 / 26 |
| 最终接受的审阅 / 规划产物 | 26 / 26 | 26 / 26 |
| 核验 Artifact / 单独复算的 Job 结果 | 819 / 52 | 819 / 52 |

v3 的 364 次响应中，260 次来自 reviewer，104 次来自 planner。同一响应可以含多个工具调用。v3 的 `gx_read_artifact` 回调有 700 次，`gx_task_context` 有 99 次。单凭这两个数字无法判定其中多少次读取冗余。公开历史汇总的 input tokens 使用 turn 粒度；统计时不将这些数字摊到各响应。[历史 v3 回执](acceptance/v3/performance/p1/summary.json) · [复算脚本](acceptance/v31/source_baseline.py)

复算命令为 `python acceptance/v31/source_baseline.py --output /tmp/gx-v31-baseline.json`。原 v3 的提交回调 p95 仍为待签收项；本次开发的测试针对修订包指定的请求数、交付、科学质量及用量指标。[原性能记录](acceptance/v3/performance/COMPARISON.md)

## 逐响应归因与中断实测

对两组历史数据再读取各自 78 份原始官方 rollout，先核对已交付的文件散列，再按响应 ID 关联逐响应使用量与实际工具。v3 的 364 个响应中，157 个归为 READ、78 个 PRODUCE、129 个 POST_SUBMIT。后者对应 3,243,664 个 token。v2.1 的 369 个响应中相同三类为 156、78、134，另有 1 个信息不足；POST_SUBMIT 对应 3,319,370 个 token。分类表示相对提交的位置，不能证明换用另一条协议就会节约相同数目的 token。直接从原始逐响应记录得到的首请求平均输入分别为 v3 的 21,085.36 和 v2.1 的 20,899.31。该补充计算使用了公开 turn 汇总之外的证据。[原始记录核对及归因](acceptance/v31/attribution/historical-attribution.json)

固定 ELF 对接本地 scripted Responses 服务，完成 204 个事务与线路探针。200 个普通用例按接受、终局拒绝各 100 个，另有 4 个故障及子代理情形。全部 204 个原 turn 都有官方取消证据，GX 正常工具回复为 0，提交后的模型请求为 0，估算结算为 0。运行中的子代理使预留保持 HELD，父用量 70，加上子代理确认的 183，最后一次结清 253。对已丢失中断命令的情形，原 turn 的权威记录仍在执行，随后对同一 turn 补发一次中断；仅丢回执的情形查询到已中断，未再发起中断。测试里的模型和 token 用量由环回服务提供，宿主和协议使用真实 Codex。整条测试运行在 7,358 个测试 token 上。[逐轮线路、账本和子代理证据](acceptance/v31/terminal-protocol.json) · [探针源代码](acceptance/v31/terminal_protocol_probe.py)

一次限额实测按 v3 原路径接入新遥测。一个 west 项目完成两项 CPU Job、一次独立审阅和一次规划决策。15 个响应均与 provider 记录匹配，分为 7 READ、3 PRODUCE、5 POST_SUBMIT。三个 turn 的首请求输入分别为 20,997、21,159、21,103 token，总用量 354,905 token，全部结清。观测工具初次启动因缺少 psutil 停止，当时没有模型预留或用量；换用系统 socket 观测后沿原项目继续，沿用已完成的两个 Job。原生运行跨度 118.269 秒，项目历时 405.233 秒，后者包含观测工具恢复时间。[实测与指标口径](acceptance/v31/waves/b0/measured.json) · [恢复前状态](acceptance/v31/waves/b0/recovery-before.json)
