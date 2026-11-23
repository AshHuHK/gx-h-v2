# GX-H v3.2 / P2 验收矩阵（待执行）

**P2 当前状态为待实测。** 审计基线为 `91ea828a8892d6e2ce125fe89cd5d5a13df18598`。下列 A01–A22 沿用实施包编号，C01–C18 对应包内 `acceptance/ADR_CONSISTENCY_CASES.md` 的原始顺序。表中“基线”只列已有能力或历史观测，“P2 判据”均须在候选版本通过正式入口取得新回执。本阶段没有启动新的科学 Run、付费模型或故障实验。[实施包原件](../GX_H_V3_2_P2_Implementation_Package.zip) · [基线证据索引](baseline-evidence.json) · [源码审计](../V32_SOURCE_AUDIT.md)

## 证据口径

| 标签 | 可支持的结论 | 应保留的原件 |
|---|---|---|
| `SCRIPTED` | 确定性合同、可控模型动作、模拟队列、故障注入及其重放。 | 输入、注入边界、GX 事务/事件、模拟后端自己的提交记录与观察结果。模拟时钟注明实际时长和模拟时长。 |
| `OFFICIAL_HOST_LOOPBACK` | 固定版官方 Codex 连接本机脚本 Responses，实测工具投递、thread/turn、Child、终止及计量协议。 | 官方版本/摘要、受控 provider、调用实参、原 turn 历史与 GX 回执。 |
| `LIVE_MODEL` | 真实 provider 上的原生 Codex 根据当时可见的 Goal、图和 Evidence 自主提出科研决定。 | 真实 Decision Session 的输入引用、模型返回的原始提案及理由、thread/turn/call、模型配置、原生终态和用量来源。授权实验额度先行冻结，逐笔区分 provider-confirmed、estimated 和仍未知的用量。 |
| `REAL_COMPUTE` | 实际运行的求解器在指定输入与计算后端上产生了数值。 | Job/Attempt、运行清单、实际后端、输入/输出摘要、原始数值及执行开销。模拟队列可以承载真实 CPU 进程，分别标注调度与计算。 |
| `INDEPENDENT_REVIEW` | 未编写求解器或生成该提案的审阅者，从原始产物复算并核对科学解释。 | 审阅者、复算方法、输入引用、数值差异、适用范围和结论。超出机械判据的 PI 签署另取人类原件。 |

同一 Run 可以同时具有多种证据。人类输入取自受信任操作员入口，记录 Goal 版本和实际发起者。程序驱动该入口的对照标为 `SCRIPTED`。真实模型开支沿用 GX 的准入与对账。已入场 turn 的最终 token 可能超过预留，报告写清可执行的硬限制、在途暴露和实际结算。GPU、真实容器和生产 Slurm 均需对应后端自己的运行证据。P2 调度崩溃用例在 LocalSchedulerSimulator 上验收。[操作员入口](../gx-h/gx_core/p0/operator.py#L20) · [既有额度与归属边界](../KNOWN_LIMITATIONS.md#L7)

## A01–A09　运行主体、插件和 Goal

| 编号 | 用例及对抗项 | 基线 | P2 判据与原件 |
|---|---|---|---|
| A01 | 全部 Codex 退出。C14 决策服务长时间不可用 | GX 已有本地 Job 对账与 reactor，多 Run 可交错。 | 对该 Run 关闭全部 Codex 宿主和子进程，保持同一 GX 服务运行并让断连持续。退出之后，GX 落库新的确定性节点结果或接收并结算完成的 Job。需要决定的范围进入持久 `WAITING_DECISION`，有通知、退避。核对进程时序、模型请求计数和前后数据库快照。只看到工作进程自行退出或最后一次 heartbeat 不足以签收。长时退避可另用受控时钟验证。 |
| A02 | C03 Agent Task Session 修改图，另测改 Goal、预算 | 工作 Root 已有 Task/Lease/thread/turn 检查。 | 向服务端分别发送有效的当前 Task 结果和越权的图、Goal、预算写请求。前者进入该任务的提交路径，后者各自被拒，权威记录无相应变更。保存请求身份及服务端结果。目录里隐藏工具并不能完成此项。 |
| A03 | C04 人类接管/撤销 Decision Lease 后迟到提交 | 已有 Task 租约回收。 | 取得合法 Decision Lease 与 base，先提交人类接管/撤销事务，再交付已生成的提案。服务端拒绝迟到调用，图 head 和分支无所请求的变更。保留撤销与提案的顺序以及原 turn 的真实费用。 |
| A04 | C01 将较弱 Goal 阈值伪装成方法参数。C02 Codex 提高硬预算 | 操作员可提交 Intent 和预算。旧引导会合并插件条目。 | 分别通过 Decision、Task、Plugin 入口尝试放宽机械判据、变更科学范围或增大硬上限，包括通过方法参数重写比较标准。核对服务端拒绝、Goal 版本/值和预算行。允许的方法参数更改只改变对应计算。合法的人类变更从同一业务所有者形成新版本，保留原批准条件。 |
| A05 | Recipe 默认值展开、精确摘要固定 | 工作流及插件已有摘要锁定。 | 从包内 Recipe 实例化 GraphVersion，读回已展开的参数、节点/边、分支、monitor、重试与停止规则、资源、seed、Goal 引用、Plugin/Node/Capability/Evaluator 精确摘要、作者与决定引用。独立重算保存的图 payload 摘要。冷开同一 head 能取得同一版本。模板占位值、未解析的实现或被覆盖的旧图均使此项失败。 |
| A06 | 插件自带新的 Capability | 包内 JSON bundle 的批准、Job 和原 Artifact 有历史用例。 | 从独立安装包通过现有 registry/verify 接口加入新 manifest，核对批准所指的精确摘要与实际 Job 的 admission 清单。更改该 bundle 或引用未经批准的版本时不能启动此 Job。保留批准、manifest 与 Job 引用。[既有用例](../gx-h/tests/test_v3_capability_attachments.py#L114) |
| A07 | C08 Node 直接提交未登记 Job。C12 隐藏无界生成/筛选/重试 | 插件按已核对字节在进程内加载。 | 给出两项合同负例，一项让 handler 直接调用模拟的 Slurm 提交入口，另一项把生成、筛选与重试藏在无上限的工作流循环中。在受控后端拦下或检测绕过 Job 所有者的提交，拒绝存在违规的节点，保存外部观察与 GX 记录。合法数值求解器内部有界时间步可运行。结论限于已审阅插件及所测路径。任意 Python 进程的行为超出此合同的证明范围。[加载边界](../gx-h/gx_core/plugins/imports.py#L30) |
| A08 | C06 Evaluator 只返回 `accepted=true` | grid@3 有领域评估和 GX 结算。 | 正例输出带 metric、有限数值、unit、语义类别、有效性及输入/producer 溯源的 Measurement，GX 用冻结 Goal 自行计算 verdict。按同一标准分别喂入达标、未达标和无效证据。裸布尔结论、单位或来源不符、缺测/非有限值均不能被记为达标。核对原始测量、GX 判定及 Evidence 引用。 |
| A09 | C07 未经批准替换 Goal Evaluator | 旧评估有冻结 spec 和输入摘要。 | 用名称或指标相同但实现摘要不同的宽松 producer 替换原 producer。在有效决定租约下也须拒绝这一改动。另测人类批准的指定替换，只对其批准范围和新版本生效，旧测量仍绑定原 producer。 |

## A10–A16　计算恢复、图变更和复用

| 编号 | 用例及对抗项 | 基线 | P2 判据与原件 |
|---|---|---|---|
| A10 | Attempt 先于外部 submit | Job 行与预算先于本地 runner，部分工作流 Job manifest 的 Attempt 为空。 | 在后端接收边界，从独立连接已能读到提交事务，至少可关联 Run、图、Node Attempt、exec_key、输入/能力摘要、资源、稳定提交标识。后端 job 名由该 Attempt 派生。以已提交数据库记录及后端接收轨迹建立先后关系。 |
| A11 | C09 调度器已接受，GX 尚未保存返回句柄就崩溃 | 真实同机 runner 有丢启动回执后找回的历史记录。 | 让独立于 GX 生命周期的模拟后端持久接受，丢失响应并终止 GX 进程，随后用新 GX 进程打开同一库。以预存身份查回原 Job、结果和费用。核对后端实际接收/启动数、GX Job/Attempt、原件与结算，原 Attempt 只执行和结算一次。另测暂时查不清时保存 `OUTCOME_UNKNOWN`，恢复查询能够推进。模糊状态下新增提交即失败。[本机基线](../acceptance/runs/jobs/lost-launch-ack.json) |
| A12 | 求解器 checkpoint 续算。C10 损坏/不兼容的 checkpoint | 已有工作流进度 checkpoint。 | 可续算的 Capability 声明支持范围并发布带摘要的求解状态。恢复创建新 Node Attempt 和后端提交身份，绑定 parent Attempt、checkpoint Artifact、兼容输入及方法。原 Attempt 的历史与费用可查。对照同一问题的连续计算。篡改字节或使用不兼容网格/方法时拒绝续算并保留原记录。 |
| A13 | C05 两个不同 amendment 使用同一 base | 有 revision 与受约束的 repin。 | 准备两个均拥有有效权限的不同草案，指向同一 head，使用各自幂等键并发提交。一次 CAS 成功，一次明确返回 stale base，只有一个新 head。冲突方读当前 head 后才能提交适用的新草案。相同调用的重投复用原回执。 |
| A14 | C13 分支等待决策时同 Run 的独立分支继续 | 原 engine 的节点等待可挂起整个 Run。 | 一个分支的声明式 monitor 先触发并等待 Codex。**在等待期间**，独立分支推进到新的节点结果或 Job 结算。对照同 Run 下的分支时间线、待决 scope 和通知。对同一 scope 重复触发时合并在途请求。并行 Run 可按已有并发上限排队。事后只展示原先已完成的兄弟节点不足以证明此项。 |
| A15 | 只更改下游节点，无关输出复用 | 旧 grid 有复用已完成 CPU Job 的记录。 | 新版只重算被更改节点及需要新输入的后继。核对两个图版本中的同一无关 Attempt/Job/Artifact 引用、开销和后端启动数。若采用在途结果，必须仍符合现行 exec_key、输入与租约，迟到且不匹配的结果不能进入新 head。 |
| A16 | C11 新 seed 夹带旧 exec_key | 旧路径键主要使用 run/path/step/input manifest/attempt。 | 在其余语义输入相同的情况下改变 seed，GX 重算 exec_key 并拒绝夹带旧键的请求。用正确身份提交新样本后，记录新的 Attempt。同一 Attempt 重投仍指向同一提交。无需借助随机数强改热方程，确定性求解器的两个数值结果可能相同。 |

## A17–A22　兼容、来源与结论

| 编号 | 用例及对抗项 | 基线 | P2 判据与原件 |
|---|---|---|---|
| A17 | v3.1 terminal、结构修复、冷恢复。C18 不支持的 Codex 版本 | 官方 0.169.0 宿主及脚本 provider 的专项有原始回执。 | 候选版本复跑原终态接受/拒绝、同 Attempt 修复、丢/迟到 ACK、commit 后跨进程恢复及账本专项。核对回执去重、原 turn 与费用、无意外模型请求。版本不兼容时显式拒绝使用优化或走已验证降级路径。保存候选版本和与原专项的逐项对照。[终态协议](../acceptance/v31/final-terminal/protocol.json) · [同 Attempt 修复](../acceptance/v31/final-public-repair.json) · [官方冷恢复](../acceptance/v31/official-terminal-crash/rc-9cd6655.json) |
| A18 | Native Child 范围、恢复与用量。C17 越界读或租约过期 | 官方宿主已覆盖开始通知丢失、普通 fork 和跨项目读取。 | 在官方宿主上重放正常 Child、开始通知丢失及冷恢复。核对允许的 Artifact 读取，拒绝无关 Artifact、过期租约、普通 fork 冒用和越权写。按真实 parent/child/turn 对账，拒绝调用不得额外创建科研产物。[Child 原件](../acceptance/v31/b2/child-recovery/final-official-lost-start.json) · [历史用量专项](../acceptance/v31/regression-final/native/native-late-child.json) |
| A19 | Brief 的摘要、大小、新鲜度和按需读取。C16 过期 Brief | 历史重放核对 338 份摘要。官方环回覆盖迟到来源。 | 逐项复核原 Artifact 字节及 SHA-256，在原单项/总量 16 KiB 边界两侧测试。大数据按引用读取。图或证据集变化后，旧 Brief 不能覆盖新 head 的工作输入。按需读回当前授权引用，可继续复用语义不变的历史原件。[简报基线](../acceptance/v31/brief-replay.json) · [迟到来源与旧 Job 复用](../acceptance/v31/brief-wire.json) |
| A20 | 独立 Plugin Graph 不依赖 science_grid | grid 插件注册 workflow 引用、工具和能力，具体领域图位于发行源码。 | 独立包携带 Recipe、至少三个可观察 Node、一个新 Capability、Tool/schema、Evaluator、两个候选分支及声明式 monitor。检查包的直接/传递导入与包内容摘要。由公开入口安装、选择并运行该包，回执指向其节点及求解器，安装前后 GX 运行时代码相同。单独注册 workflow 名称或只回放旧 grid 数据不能签收。[旧注册入口](../gx-h/plugins/installed/org.gx.science-grid/3.0.0/plugin.py#L45) |
| A21 | 后端拒绝未批准的容器摘要 | 环境可记载 container digest。 | 在模拟后端的准入边界，对其他参数相同的请求核对已批准 digest 的放行与替换 digest 的拒绝，后者的后端启动数为零。若后端对所有容器一律报“不支持”，本项仍待验。报告注明测试针对 digest 准入。实际容器/GPU/Slurm 执行另据实记录。[原环境字段](../gx-h/gx_core/capabilities/environments.py#L151) |
| A22 | C15 代理指标通过、缺少正交证据 | grid 的历史科研结论限于其数值问题。 | 用明确标识为 model-derived proxy 或 heuristic 的测量达到机械标准，核对 GX 的 computational status 和单独的人类科学签署状态。缺乏所需正交证据时该科学结论保持待签。历史独立审阅、官方宿主或模型自述均不得填作 PI 签署。可用有标签的合成测量完成这个合同测试。 |

## 一次未来实测如何贯通科研与执行

1. **冻结人类问题。** 操作员入口接收 Human/PI 的目标、有限问题范围、机械判据、获准的 Goal Evaluator、资源上限与科学签署范围，获得可追溯的 Goal 版本。热方程的网格、初边值、扩散系数、终止时间、精度/误差范数、容差和允许改动的参数在采集前写明。具体数值由选定项目给出。本矩阵不预填项目预算或合格阈值。
2. **Codex 选择方法，GX 建立初始图。** 从同一 Run 真正启动有预算的原生 Decision Session。它依据冻结 Goal 读取已安装的可用 Recipe，提交选用或实例化草案。GX 核对实际会话、Decision Lease、base 和插件摘要，展开默认值并创建初始不可变 GraphVersion。工作 Task 的工具只接受其任务产物。保存原始模型调用、图 payload 和事务回执。包也允许人类选择 Recipe。若这一轮由人选择，应在回执中标明实际作者。
3. **实际执行 FTCS 与 ADI。** 采用有限二维热方程的本机 CPU 实验。一个可选参考问题是在单位正方形的均匀直角网格上取常数扩散系数 α、零 Dirichlet 边界和初始场 `sin(πx) sin(πy)`，时刻 T 的解析场为 `exp(−2π²αT) sin(πx) sin(πy)`。两项真实 Job 使用相同物理问题、约定采样点和终止时间。FTCS 记录步长与二维稳定条件 `αΔt(1/Δx² + 1/Δy²) ≤ 1/2`。ADI 记录实际采用的方向隐式步骤。保存输入网格、原始数值场、参数、代码/能力摘要与执行清单。LocalSchedulerSimulator 负责可控的队列回执。若仅返回预写数值，则此轮只获得 `SCRIPTED`。
4. **从原始场得到证据。** 插件 Evaluator 对解析参考计算已约定误差，报告语义类别 `numerical_verification`、单位、有效性和来源。GX 用人类 Goal 判定并登记 Evidence。独立审阅者直接读取原始场、网格和时间，另行计算同一误差、核对两个 Job 及材料引用，记录差异与有限适用范围。两个求解器给出相近数值或 evaluator 照录 worker 自报误差，都不足以支持独立复算。
5. **让观察决定 amendment。** 各分支先独立保存度量。初始 GraphVersion 已声明的 monitor 在两支汇合前依据本分支的度量触发，使独立分支在决策等待期间仍有工作可做。Codex Decision Session 读取已产生的支持/相反 Evidence、当前图 head、Goal 和余额，在授权范围内提出有依据的修订，例如调整某一分支的离散参数。GX 校验租约和 base 后以 CAS 提交，生成新 GraphVersion 及受影响节点集合。保留原生模型实参及 GX 的接受/拒绝结果。若模型选择停止、证据未触发或预算已耗尽，记录实际决定，真实 amendment 链路仍待验。
6. **继续同一 Run。** 在同一项目执行新版本，记录重跑分支的新 Attempt/Job/Measurement/Evidence 和复用分支的原引用。GX 在没有 Codex 的阶段仍可推进。恢复打开后能从原图 head 和已落库工作继续，在最终报告中给出两个方法在这组输入和判据下的观测、开销与不确定性。需要 PI 解释的物理结论保留人类签署状态。原始命令、构建/插件/输入摘要、Run 与版本、原生会话、预算、事件、Job、Artifact、复算与结算应可相互定位。

## 两个相互独立的停机对照

| 故障点 | 操作与观察窗口 | 通过证据 |
|---|---|---|
| **仅退出 Codex（A01、A14）** | 让该 Run 的 Codex 宿主全部退出，保持 GX 和计算后端可用。验证窗口从最后一个宿主退出开始，到下次允许启动宿主为止。安排一个范围等决定，另一个范围仍有确定性工作。 | GX 在窗口内持久记录新结果和等待/通知。旧、当前两个快照与分支时间线能证明进度。之后按原 Run 和 scope 唤起决策，使用当时的 head 和剩余预算。 |
| **仅在后端已接受窗口终止 GX（A10、A11）** | 保持模拟调度器及其持久接受记录可用，在 GX 收到/保存句柄之前使 GX 进程实际退出。新 GX 从同一库开始，依据原 Attempt 的稳定后端身份查询。 | 模拟后端的接收、实际执行和结果可以对应到原 Attempt，GX 单次结算。后续重开复用同一结果。单纯重开已完成项目、只令 API 抛异常或在科学提交 commit 后退出，各自覆盖其他边界。 |

求解器暂停后从数值 checkpoint 续算（A12）另有父子 Attempt 与等价计算判据。一项耗时的真实模型采集可与多个断言共用。故障时序和越权输入使用确定性夹具即可。

## 回归、比较与签收

- Phase 2 以本机合同、模拟后端及官方环回专项验证确定性行为，同一候选须通过全套 v3.1 离线及官方环回回归。历史真实付费科研试验保留原回执。Phase 4 在同一冻结候选上完成原回归、v3.1 terminal/repair/native child 专项和新增 ADR 用例，并列出原有可选项的实际通过/跳过/失败。旧项目用副本验证固定版本、原数值与原账本的恢复。历史全量测试为 446 项 Python（442 通过、4 项可选项跳过），另有 P0 127 项、Node 2 项及官方专项。这些是历史分母。[原回归](../acceptance/v31/regression-final/summary.json) · [冻结比对](../acceptance/v31/regression-final/frozen-comparison.json) · [历史项目冷恢复](../acceptance/v31/final-at2-cold.json)
- 性能比较沿用同一既有工作负载、输入、并行度、宿主/provider 模式与统计口径，列出完成科研产物、响应/turn、后端启动、已确认与估算 token、执行时长及样本量。新热传导任务另报自身实测。已有 B3 对照和 B5 的四个真实原生 turn、三个 CPU Job 可供定位历史行为。本矩阵不指定新的固定波数或性能阈值。[历史测量方案](../acceptance/v31/waves/measurement-plan.json) · [历史对照](../acceptance/v31/waves/comparison.json) · [真实 B5 回执](../acceptance/v31/quality/final-live/evidence.json) · [B5 独立复算](../acceptance/v31/quality/independent-review-final.json)
- 签收报告按 A 编号关联候选提交、实际环境、命令和原件，逐项标明已通过、失败、未执行或仅覆盖部分。代理指标用例只给出所测的机械结论。独立插件实跑、真实模型修订、两类停机和部分重算各依本次相应证据判定。缺少其中一段时保留该段及整体 P2 的待验或部分完成状态。
