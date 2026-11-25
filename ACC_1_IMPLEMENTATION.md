# 审阅修复与分批验收

本次从 `47165af6befa919a5fc444a7c823ea9160cfb22f` 开始实施。修前在同一 checkout 重现了审阅指出的六种现象。业务入口是 `bin/gx-p0`。官方 Codex 0.169.0 拥有执行会话和短期 child，GX SQLite 拥有正式需求、长期任务、租约、Job、费用和研究结果。[修前原始记录](acceptance/runs/acc-1-before-fix/observations.json) · [计划与用户补充的签收标准](FIX_PLAN_ACC_1.md)

最终产品提交为 `61c70767ca09eaa6775bb16f16c8a56ab0f71c04`。第一批通过已测的等待、交接和故障恢复，第二批通过多轮需求、来源复评与原生 child 协作，第三批以首个真实领域项目的正式科学决策签收。该项目有一笔历史原生用量待对账。扩展中已实际执行两个 Workflow 和同一厂商的两个模型，第二个生产厂商仍未配置。[真实研究中文决策](acceptance/runs/acc-1-research/oauth-v2/decision-zh.md) · [研究对照与历史失败](acceptance/runs/acc-1-research/summary.json)

从最终 Git 提交导出的独立副本运行 Python 全量回归，共 277 项，273 通过、0 失败、0 错误，4 项可选 DSH 测试因缺少对应运行时跳过。P0 专项 106/106、账本与资源专项 46/46、Node 插件 2/2，角色生成校验通过。六项受变更影响的 GX 与官方 App Server 故障场景全部重跑通过。最终发布代码再次打开实际线上项目，原报告、四个 scheduler ref、三份已接受科学提交以及四笔原生 turn 的账本状态相同，新增费用和 Job 都为零。[冻结版本回归与命令](acceptance/runs/acc-1-regression/README.md) · [线上项目终态重开](acceptance/runs/acc-1-research/oauth-v2/final-reopen.json) · [逐工单状态](acceptance/runs/acc-1-status.json)

## 第一批 · 等待、交接、计量与恢复

| 工单 | 实施与观测 |
|---|---|
| BUG-01 | Driver 在临时准入等待时继续轮询。两个已领任务分别保留原 Attempt，A 预留 100、实耗 35，B 随后预留 65、实耗 35。调用次数或 token 确已耗尽时记录暂停原因。token 耗尽场景中已经在跑的 CPU Job 正常完成。重开没有新收费调用。[争用](acceptance/runs/acc-1-driver/contention.json) · [token 耗尽](acceptance/runs/acc-1-driver/token-exhaustion.json) · [调用次数耗尽](acceptance/runs/acc-1-driver/call-exhaustion.json) |
| BUG-02 | 在同库事务中登记主动等待和 Job 终态交接。回收依据持有者、当前 Attempt 与等待原因。测试推进了租约和交接时间，并在 Codex 已接受 wake、GX 尚未保存 ACK 时实际 SIGKILL GX。冷启动沿官方同一输入回执找回原 turn，只继续一次、收费一次，保留原 Job 和 Attempt。[事故时序、命令及实测边界](acceptance/runs/acc-1-protocol/README.md) · [超期后唤醒](acceptance/runs/acc-1-protocol/expired-wake.json) |
| BUG-03 | 锁定版 App Server 的累计数字可能重复前次值。GX 比对逐响应计量、官方线程与 turn 身份和 child 归属。末次用量缺失、重复收到 35 的用例只保留原预留；另一用例中父 70、晚到的 child 183 合并结算 253 一次后才允许下一笔付费。[缺失末次用量](acceptance/runs/acc-1-protocol/gx-missing_last_usage.json) · [晚到 child](acceptance/runs/acc-1-protocol/child-late.json) |

同一源码对 15 个组合等待用例运行两次。覆盖超期、用户暂停、真实需求撤销、旧库迁移、回执晚到及静默执行失败。涉及长期限的用例推进数据库时间，真实进程故障、CPU 计算与跨进程账本对账分别留证。[精确命令和结果](acceptance/runs/acc-1-waits/observations.json)

首次真实 OAuth 坐标研究还暴露了跨 Attempt 的计量缺口。同一审查任务失败重试时换了线程，旧线程的费用尚未到账。现在重试保留自己的新租约，等待同一任务的旧账。预算允许的独立任务仍能执行。公开 Driver 的并行用例中，另一任务及其后继先完成，被拒任务的第二次 Attempt 此时新收费为零。旧回执实耗为 15 时可继续，实耗为 70 且结算后项目满额时正常暂停。相同工作负载下，提前重试会花到 130/100，修复后为 100/100。两种结局各复跑两次，冷重开均无新模型请求。[修前修后对照与运行命令](acceptance/runs/acc-1-attempt-meter/comparison.json) · [原 OAuth 逐笔时间线](acceptance/runs/acc-1-research/oauth/budget-admission.json)

`stop` 提交暂停状态后立即返回。后台驱动接收已准入 turn 的末次回执，再关闭官方连接。用例中，原版暂停时只看到中间用量 210，留下待对账记录。修复版在暂停后人为扣留回执三秒，驱动等到最终 245 再结算，重开没有新模型请求。[同一时序修前](acceptance/runs/acc-1-pause-drain/before.json) · [修后](acceptance/runs/acc-1-pause-drain/after.json)

预算按操作所需资源准入。第 4 个 Job 完成后仍可读取它的结果并用已有模型额度进行审查。真正需要第 5 个 Job 时，Job owner 保留其明确拒绝。验证还覆盖预算只够两个拟提交 Job 中的一个时，已入场的 Job 先运行到终态，后一个新 Job 仍受限。[资源维度与原库恢复](acceptance/runs/acc-1-waits/resource-dimensions.json)

## 第二批 · 需求、来源、任务执行与审查

- **BUG-04、BUG-06**。新任务书绑定实际适用的条目；通知携带不可变的条目值、版本和删除语义。当前 Worker 可以读取作用域内的最新任务上下文。已发送的旧通知用原正文对账。当前版本的输出偏好、选择优先级及优化目标有消费或明确的适用范围。[变更处置](gx-h/gx_core/p0/impact.py) · [任务上下文和提交检查](https://github.com/AshHuHK/gx-h-v2/blob/47fc728d32bc10ba478df6a063529a8d98f98828/gx-h/gx_core/p0/science_inputs.py)
- **BUG-05**。在旧审查交卷时从 CLI 补充“先前对照可能受污染”，留下原审查、一次后继独立审查和一次决策。这是纯计算样本，后继审查与决策均能引用原文并解释为什么维持数值判断。两份原 CPU Job 得到复用。状态的 `reviews` 与 `supplements` 可分别追查实际审阅集合与处置。对冻结的旧 `science_grid@1`，缺少消费证明的补充保持待审。[官方 App Server 加脚本模型的全链实测](acceptance/runs/acc-1-source-official/summary.json) · [事务顺序与恢复](acceptance/runs/acc-1-source-judgments/) · [执行代码](https://github.com/AshHuHK/gx-h-v2/blob/47fc728d32bc10ba478df6a063529a8d98f98828/gx-h/gx_core/p0/science_inputs.py)
- **GAP-01**。自然文本经真人回执后逐项核对具体授权。夹带的其他编辑留作提案。需要确认的候选绑定用户已看到的版本、内容和后果。降限影响未来准入，历史实耗不改，取消在途 Job 有单独的后果预览和确认。[公开 CLI 对话](acceptance/runs/acc-1-natural-input/summary.json) · [三个额度区间](acceptance/runs/acc-1-natural-input/budget-ranges.json)
- **GAP-04**。主循环在工具回调与事务外核验官方父子关系。child 的每次读取重新检查父任务、Attempt、租约和产物范围。原工作线程可以在初次验证前或冷恢复后收到可重试结果，验证成功后读取原文。跨项目、普通 fork、旧租约及写操作分别测试。[官方 child 实测](acceptance/runs/acc-1-child-access/official.json)

## 第三批 · 有边界的真实领域研究

**扩大预算后的真实 OAuth 项目已经完成。** `gxr_01m816989yd1bcmpyp` 从原始 2RE9 坐标、问题和资源上限开始。正式根预算为 12,000,000 token、18 个 GX turn 和 4 个 CPU Job，最多两轮。Astra 的方案、Sol 的独立评审、另一个 Astra 线程的决策全部在第一次提交获验收。首轮即测完三种选择，决策者依据独立评审停止。最终报告为 `gxa_01m816vzpv0r8veg2f`，系统状态为 `COMPLETED`。四个 Job 的 ID 在停机恢复和终态重开后保持一致，终态重开新增模型 turn 为零。[真实运行、决策与终态重开](acceptance/runs/acc-1-research/oauth-v2/summary.json) · [原始科研产物](acceptance/runs/acc-1-research/oauth-v2/products.json) · [根账本与正式需求](acceptance/runs/acc-1-research/oauth-v2/initial-budget.json)

此次真实项目在拟合前冻结了覆盖下限 49/164、角度差 0.50°、RMSD 差 0.15 Å，以及独立复算容差 0.01° 和 0.001 Å。两条全覆盖路线的三个链对都在界限内。有序主链只保留 49 个残基，涉及它的六个比较均越过至少一条界限。共同 Cα 与有序主链比较的最大角度差为 0.747°，最大 RMSD 差为 1.217 Å。九次独立拟合的数值差均在复算容差内。报告将“对原子选择敏感”的结论限定在该沉积结构的已观测坐标。[判据与复算明细](acceptance/runs/acc-1-research/oauth-v2/recomputed.json) · [已接受的评审与科学决策](acceptance/runs/acc-1-research/oauth-v2/scientific-status.json)

运行开始于产品提交 `adceab86`。第四个 Job 完成时，旧引擎因 Job 额度用尽把评审也暂停了。修复后的代码 `316a8a3b` 在原库回收四份结果并完成研究。此过程留下明确的计量边界。三笔 turn 已结算 599,701 token，其中最终决策为 185,017。早期中断的规划 turn 已观测到五份响应共 162,230 token，缺少足以证明第六份最终用量的回执。该笔保留 666,666 token 和一次 GX 调用的预留。公开状态同时给出正式科研结论和这笔未决费用。[停点与原 Job 引用](acceptance/runs/acc-1-research/oauth-v2/job-quota-handoff.json) · [计量与持有记录](acceptance/runs/acc-1-research/oauth-v2/pending-usage-after-cold.json)

交付场景是 2RE9 实验晶体坐标的几何稳健性分析。Run 从目标、来源和资源上限生成假设、冻结判据、执行真实 CPU Job，然后交独立研究员复算和审查。公开输出包括坐标 SHA、覆盖分母、链间几何及容差、实际评审提交、下一步决策和原始 Job 引用。主要计算使用 Python 标准库，独立复算使用 Biopython/NumPy。这一范围限于这些观测坐标。[启动步骤](README.md) · [原始资料、算法、覆盖和独立复算](gx-h/adapters/scientific/tl1a_geometry/README.md) · [清洁工作目录的 CPU 实测](acceptance/runs/acc-1-coordinate-method/clean.json)

两条完整选取路线都覆盖 164 个观测残基。共同主链与共同 Cα 原子选择间，三个链对中最大的角度差为 0.06439°，拟合 RMSD 差为 0.03988 Å。更严格的有序主链选择覆盖 49/164 个残基。它是否有足够覆盖、能否支持原子选择敏感的判断，按每次实验在拟合前冻结的标准解释。主程序和独立程序对三种方法、三个链对共九次拟合，角度差至多 1.85 × 10⁻¹³°，RMSD 差至多 1.41 × 10⁻¹⁵ Å。[数值、覆盖和独立复算容差](acceptance/runs/acc-1-coordinate-method/clean.json)

使用官方 App Server、脚本模型、真实坐标和 CPU 的两个对照分别走过两种决策。一例在首轮结果稳定后，自主流程按脚本模型提出的未测路线再计算，第二轮按预先冻结的标准得到敏感并停止。另一例在还有一个 Job 额度时提前结束。两例均在有在途 Job 时暂停、恢复，并在终态重开后核对新增计费 turn 与新增 Job 均为零。质量评估 Workflow 发布的选取产物是几何 Workflow 实际消费的输入。[两轮产物与决策](acceptance/runs/acc-1-research/scripted-followup/summary.json) · [提前停止](acceptance/runs/acc-1-research/scripted-stop/summary.json) · [独立复算产物](acceptance/runs/acc-1-research/scripted-followup/recomputed.json)

**第一次真实 OAuth 运行在独立审查节点暂停。** 运行 `gxr_01m8157egct7y5p1c0` 使用 OpenAI 的 `gpt-6.2-astra` 规划和 `gpt-6.2-sol` 复核，两者均使用验收档 `xhigh`。研究员事前选择三种方法，将严格路线的覆盖下限冻结为 49/164。四个 CPU Job 全部成功。reviewer 从原始坐标复算了三种方法的九个链对拟合，两份实际报告均判断严格选择会改变拟合。旧工作流第一次因“局限”为列表拒收，第二次因用 GX 的正式假设 ID 提交判断拒收。项目最终保留两份原报告及拒收理由，GX 的正式审查、下一步决策和报告均未生成。[原始项目与命令](acceptance/runs/acc-1-research/oauth/summary.json) · [原始审查、结算与暂停位置](acceptance/runs/acc-1-research/oauth/scientific-status.json) · [按工具统计的上下文](acceptance/runs/acc-1-research/oauth/tool-context.json)

该项目五个原生 turn、42 份服务端响应最终累计报告 1,618,296 token，其中 1,399,040 为缓存输入。五笔全部结算，剩余预留为零。项目的 1,000,000 token 上限按准入预留执行，实耗超过上限 618,296。四次科学 Attempt 各读取了整份坐标的 22 页，共向模型返回了 2,031,884 字节坐标正文。工具字节数不能直接换算为账单。公开接口没有返回美元金额。原项目保留该支出与暂停状态，后续离线复核没有新增外部推理或重跑原 Job。[原始用量与资源状态](acceptance/runs/acc-1-research/oauth/scientific-status.json) · [响应与原文读取统计](acceptance/runs/acc-1-research/oauth/tool-context.json)

新的 `tl1a_geometry@2` 为简报附上来源摘要、原文位置、坐标样本与产物格式。研究员按需读取原文，复算工具仍从完整原始坐标计算。审查允许用文本或文本列表陈述局限，可用一套完整且唯一的预测短名或 GX 正式假设 ID 逐项提交。用只读数据库复核旧运行的两份原始提交，两份均通过新规则，数据库写入数为零。新版本的两轮回环对照还核实结算确实指向对应的正式假设。[两份原件只读复核](acceptance/runs/acc-1-research/reviewer-compatibility/readonly-replay.json) · [新项目原文定位与逐假设结算](acceptance/runs/acc-1-research/scripted-followup-v2/source-and-review-shapes.json)

## 扩展与移交

保留原 `science_grid@1` 的科学代码与固定摘要。新 grid 项目使用 `science_grid@2`。两者通过相同的三轮公开入口回归，各有四个 CPU Job、六项审查/决策提交、七个官方 turn、1,400 个脚本 token，终态重开零新增。另从旧 CLI 启动项目，在一个 CPU Job 运行时升级，新 CLI 在原数据库中接回相同 scheduler ref。32 个已有产物、9 项记账事件和 2 份已接受提交全部保留。旧项目明确报告无法按 v2 规则审查的新来源。[原版](acceptance/runs/acc-1-grid/reference-47165af.json) · [修复版](acceptance/runs/acc-1-grid/final.json) · [实际在途升级](acceptance/runs/acc-1-grid/upgrade.json)

GAP-03 已接通按角色选 provider、model、effort 和 GX 工具。分别经过两个环回 Responses 端点的官方运行、暂停、恢复和计量。其结果用于确认路由。当前部署没有配置第二个生产厂商，第三方实际推理、冷恢复和计费仍待测。[角色配置与启动](MODEL_PROFILES.md) · [公开入口路由](acceptance/runs/acc-1-profiles/summary.json) · [本机配置证据](acceptance/runs/acc-1-profiles/local-configuration.json)

当前部署 Prompt 选定官方 Codex App Server。ACC-1 的 2RE9 研究只在这个后端运行。原 GX 和 Codex 源码宿主都没有同输入、同预算、同评价标准的 2RE9 运行记录，因此本次不报告三后端的科研质量、在线费用或性能排名。[部署 Prompt 的运行时约束](GX_Codex_Implementation_Agent_Prompt.md)

部署与回退使用独立状态目录。升级前暂停新准入，记录在途 Job 及 operation key，用 SQLite backup 或正常停机后的一致快照保存状态库与产物。使用副本读取旧历史、保留时点和原费用。回退时对账升级后已产生的外部工作，不用旧库重新发同一实验。发布物包含固定 Codex 源码、Schema、原始科学材料、独立参考和离线依赖。[发布与干净环境复验](RELEASE.md) · [部署说明](DEPLOYMENT.md)

### 逐项交接

在本交付 checkout 用 `git diff 47165af..61c7076 -- <路径>` 可读取各工单的实际差异。基础修复在 `edee0b5`，真实研究暴露的重试、审查和资源维度问题分别在 `adceab8`、`316a8a3` 和 `61c7076` 集成。

| 工单 | 从哪个业务入口读差异 | 对应复验入口 |
|---|---|---|
| BUG-01、02、03 | `gx-h/gx_core/p0/{driver,roots,root_delivery,waits}.py`、`gx-h/gx_core/budgets`、`gx-h/gx_core/jobs` | `acceptance/acc1_driver.py`、`acc1_wake.py`、`acc1_cold_meter.py`、`acc1_child_late.py` |
| BUG-04、05、06 | `gx-h/gx_core/p0/{impact,science_inputs,science_refresh_v2}.py`、`gx-h/workflows/science_grid_v2` | `gx-h/tests/test_p0_review_sources.py` 与公开 CLI 来源验收记录 |
| GAP-01、04 | `gx-h/gx_core/p0/{intent,intent_text,intent_budget,roots}.py` | `acceptance/acc1_intent_text.py`、`native_child_access.py` |
| GAP-02 | `gx-h/workflows/tl1a_geometry`、`gx-h/workflows/tl1a_coordinate_quality`、`gx-h/adapters/scientific/tl1a_geometry` | `acceptance/acc1_research.py`、`acc1_coordinate_method.py` |
| GAP-03 | `gx-h/gx_core/p0/profiles.py`、`gx-h/gx_core/hostobs/host_policy.py` | `acceptance/profile_routing.py`、`bin/gx-p0 profiles --state DIR` |
| DEL-01 | `scripts/build_release.py`、`scripts/release_assets.py`、`release-assets/` | `scripts/smoke_release.py` 及附件旁的校验清单 |
