# GX-H v3.2 / P2 源码审计

**审阅结论。** v3.1 已有可恢复的科研执行引擎、Job 服务、租约、预算和证据存储，值得沿用。公开入口还没有把第三方 Recipe 实例化为项目自己的 GraphVersion；Codex 不可用时也没有保证同一 Run 的独立分支继续。P2 的目标链路尚不能在基线签收。

## 审计对象和证据

源工作树 `gx-h-v-3-2` 的 HEAD 和本地已核实的 `origin/v3.1` 都是 `91ea828a8892d6e2ce125fe89cd5d5a13df18598`，源工作树干净。指定的 `gx_v_3_2_persistent` 起初只有实施包。在这里从该提交建立独立本地分支 `v3.2-p2`，保留原包。ZIP 的 SHA-256 为 `849a0593808aa90e01a748fc14ea1df83a1c4dc50a544b9482e75f6da480a631`。与历史验收产品 `9cd6655` 比较，产品、脚本、示例、适配器及发行资产目录无差异。本次从提交对象核验已有回执和源码，没有调用科研模型、运行新的 P2 故障测试或改写数据库。下文行号均指这个基线。[包原件](GX_H_V3_2_P2_Implementation_Package.zip) · [原件摘要、核查命令与历史回执字段](p2/baseline-evidence.json)

## 从正式入口追到各所有者

| 环节 | 源码所证明的现状 | P2 缺口或复用条件 |
|---|---|---|
| Human Goal / Budget | `gx-p0 start` 取 objective、版本、插件和起始 hook；带 run key 时本地提交初始 Intent，无 key 的旧路径可在第一个 checkpoint 前需要主线程。人类输入、版本、预览和预算明确分表。 [启动](gx-h/gx_core/p0/cli.py#L104) · [引导](gx-h/gx_core/p0/cli.py#L143) · [Intent](gx-h/gx_core/p0/intent.sql#L6) · [人类预算预览](gx-h/gx_core/p0/operator_budget.py#L80) | 引导会合并插件默认值；不能把 hook 生成的目标或阈值视为 PI 授权。现有确认条件未普遍覆盖 evaluation_spec 更换。运行时还存在按政策批准预算的通道，须明确定义它与 PI 硬上限的关系。[确认条件](gx-h/gx_core/p0/intent.py#L373) · [既有治理路径](gx-h/gx_core/orchestration/service.py#L1904) |
| Codex 决策和 Task 会话 | 有 run 主线程、带 task/attempt/lease 的工作 Root 和 provider 用量。工作调用绑定真实 thread、turn 与活租约。主线程服务人的消息与 Intent，planner 提交的是科研 Task 的产品。[Root 绑定](gx-h/gx_core/p0/roots.py#L69) · [主线程](gx-h/gx_core/p0/roots.py#L760) · [提交入口](gx-h/gx_core/p0/roots.py#L977) | 主线程、planner、协调器租约都不具备所要求的 scoped Decision Lease、base graph head、接管和 amendment 合同。Task 与长时决策的权限边界需扩展。 |
| Recipe / Node / Plugin | 现有插件包可核对内容摘要、依赖闭包和贡献项，注册 workflow 引用/hooks、工具及 schema、能力引用或包内 JSON manifest。启动先从 shipped registry 取得图，再绑定插件。已安装的 grid 插件注册引用并提供读取工具；图、节点及对旧工作流的依赖在发行源码里。[注册入口](gx-h/gx_core/plugins/author_api.py#L196) · [加载核对](gx-h/gx_core/plugins/loader.py#L56) · [运行时绑定](gx-h/gx_core/plugins/runtime.py#L80) · [公开入口](gx-h/gx_core/p0/cli.py#L121) · [grid 插件](gx-h/plugins/installed/org.gx.science-grid/3.0.0/plugin.py#L45) · [实际图](gx-h/workflows/science_grid_v3/__init__.py#L14) | hello 工具示例与独立 capability 均不能证明包内图可运行。manifest 枚举出现 evaluator 也不等于加载器提供该注册能力。现行可执行图 loader 与实施包 JSON 模板有不同字段。[图准入](gx-h/gx_core/orchestration/service.py#L117) · [图解析](gx-h/gx_core/orchestration/loader.py#L37) · [能力原有用例](gx-h/tests/test_v3_capability_attachments.py#L114) |
| GraphVersion / Workflow Runtime | 注册表拒绝同版本不同内容，Run 持有工作流摘要。Engine 保存 checkpoint，按照拓扑顺序运行节点，核对中断、revision 和预期产物再恢复。原有 repin 有在途租约、Job、预算的全局静止条件。[注册表](gx-h/gx_core/orchestration/registry.py#L32) · [Run pins](gx-h/contracts/db_schema.sql#L25) · [恢复](gx-h/gx_core/orchestration/engine.py#L925) · [旧 repin](gx-h/gx_core/orchestration/repin.py#L43) | 缺少每 Run 的不可变实例图及 head CAS。现有循环挑第一个剩余节点，遇等待可停整个 Run，rewind 清掉拓扑后缀。要保留无关分支，须扩展这个 Engine 的 ready/park/复用逻辑。[执行](gx-h/gx_core/orchestration/engine.py#L1380) · [回卷](gx-h/gx_core/orchestration/engine.py#L1676) |
| Attempt / Job / Checkpoint | 原生 Task 用 lease 和 Attempt 提交 Job proposal，经 outbox 转交 JobService；工作流节点还可直接委托该服务。Job 行、预算和预期输入先于本地进程，已有同机 detached runner、磁盘回执、PID 与启动时刻对账和未知结果处理。[原生 proposal](gx-h/gx_core/p0/compute.py#L85) · [节点路径](gx-h/gx_core/jobs/workflow.py#L10) · [本地提交](gx-h/gx_core/jobs/service.py#L674) · [本机身份](gx-h/gx_core/jobs/runner.py#L892) · [恢复](gx-h/gx_core/jobs/service.py#L1486) | 部分工作流执行的 manifest 中 attempt_id 可为空；当前路径复用会参考 run/path/step/input manifest，未覆盖所有 seed/参数。scheduler_ref 是事后获得的本机句柄。引擎恢复用的 checkpoint 与求解器可验证的数值重启文件用途不同。[所有者差异](gx-h/gx_core/p0/manifest.py#L41) · [旧复用键](gx-h/gx_core/jobs/service.py#L787) · [引擎 checkpoint](gx-h/contracts/db_schema.sql#L1013) |
| Artifact / Evidence / Evaluator | Artifact 带内容摘要和来源；已有 eval specs、evaluations、evidence 以及决定与产物引用。grid@3 校验原始证据并计算确定性字段，条件成立时免独立审阅，其余情况由 reviewer 填写有界判断。[Artifact](gx-h/gx_core/artifacts/service.py#L528) · [证据表](gx-h/contracts/db_schema.sql#L651) · [冻结评估](gx-h/workflows/science_grid_v3/products.py#L46) · [条件审阅](gx-h/workflows/science_grid_v3/nodes.py#L125) · [结算](gx-h/workflows/science_grid_v3/settlement.py#L21) | 旧领域 settlement 的 `supported` 和规划产品不能充当通用 Measurement 或 PI 签署。独立插件缺少带单位、语义类别、有效性和 producer pin 的度量入口，以及 GX 对 Goal 的单独判定。 |
| GX 单独继续 | 有本地推进、Job 对账、reactor、workspace 多 Run 轮询和传输重试。首次需要原生服务时才建立连接；启动缺少 Codex ELF 可以抛出不在 workspace 恢复分支中的异常。即使已启动，消息和旧用量处理也可能先于确定性工作。[Driver](gx-h/gx_core/p0/driver.py#L147) · [工作次序](gx-h/gx_core/p0/driver.py#L378) · [workspace 循环](gx-h/gx_core/p0/coordinator.py#L137) · [进程启动](gx-h/gx_core/hostobs/app_server_client.py#L167) | 已证明的回执恢复会再启动官方宿主；没有基线证据证明所有 Codex 退出时同 Run 的无关分支仍能产生一个新结果，或能跨 GX 重启消除后端已接受但未保存句柄的歧义。 |

## 六条 ADR 的基线状态

标记按子能力使用。每条 ADR 整体均为 **partial**；以下列明已经实现的部分和阻碍整体签收的差异。

| ADR | implemented | missing / conflict / deferred |
|---|---|---|
| 01 | GX 持有 Run、工作产物和实际 Task 租约。 | **missing** 决策租约、人类接管及按分支 WAITING_DECISION。**conflict** 主线程角色和协调器身份无法代替 Graph 决策权限。 |
| 02 | 包锁定、工具及能力绑定、Job 本地持久化。 | **missing** 包内 Recipe/Node/独立 Evaluator 的实例化，外部后端按预先身份找回、求解器 checkpoint 血缘。**deferred** Slurm 实机。 |
| 03 | Intent 版本、历史批准、额度与在途预留、冻结评估和 evidence。 | **conflict** 领域阈值可来自 proposal，初始化与既有预算批准路径不能授予插件或模型 PI 权限。**missing** 两层 Goal、producer 变更授权和结果绑定的科学签署。[原阈值来源](gx-h/gx_core/p0/science.py#L134) |
| 04 | 工作流源码 pin、checkpoint、任务重试和幂等。 | **conflict** 全 Run repin/回卷与部分冻结，旧路径键与 seed 语义。**missing** Run 实例图、节点级语义键及活决策租约下的 CAS。 |
| 05 | 接受与终局拒绝同事务保存裁决和回执；取消未答工具，查询原 turn；结构错回滚后另事务去重，同一 Attempt 修复。固定版启动校验和累计用量对账已有。[协议](gx-h/gx_core/p0/terminal_products.py#L75) · [修复表](gx-h/gx_core/p0/zz_v31_terminal_products.sql#L14) · [直接调用用量](gx-h/gx_core/p0/metering.py#L89) | **missing** 面向两类 Session 的版本化适配边界。**conflict** 现行优化按 grid@3 与直接提交工具识别；插件工具可转交 _submit，但该入口没有相同的直接调用身份。**deferred** 未验证的 Codex 版本。[版本校验](gx-h/gx_core/p0/roots.py#L149) · [插件结果转交](gx-h/gx_core/plugins/runtime_context.py#L165) |
| 06 | 角色目录、主循环核验 native child 来源，回调内重验租约及可读引用，16 KiB 单项/总量的不可变简报预载及延迟读取；已有结构化恢复历史。[角色](gx-h/gx_core/p0/profiles.py#L162) · [child](gx-h/gx_core/p0/root_delivery.py#L874) · [简报](gx-h/gx_core/p0/grid_brief.py#L74) · [历史](gx-h/gx_core/teams/recovery.py#L203) | **missing** 将授权与新两类 Session、分支和 Graph head 绑定，通用只读上下文和重读契约。原简报及工具策略的一部分是 grid@3 特化。 |

## 证据适用范围和信任边界

- **OFFICIAL_HOST_LOOPBACK**。在固定 Codex 0.169.0 的既有 204 项协议试验中，接受 103、终局拒绝 101，终局后的正常工具回复为 0。最终真实跨进程崩溃样例在原科学提交事务之后退出，冷恢复额外模型请求为 0，105 个测试 token 只结算一次。它不能覆盖外部 scheduler 刚接受作业的崩溃窗口。[协议](acceptance/v31/final-terminal/protocol.json) · [冷恢复](acceptance/v31/official-terminal-crash/rc-9cd6655.json)
- **LIVE_MODEL / REAL_COMPUTE / INDEPENDENT_REVIEW**。历史 grid 研究有 4 个原生 turn、3 个 CPU Job 和独立数值复算。它检验固定图内两轮科学判断，没有热传导插件和图 amendment。四个既有可选 DSH 测试的跳过保持原记录。[实测](acceptance/v31/quality/final-live/grade.json) · [独立核对](acceptance/v31/quality/independent-review-final.json) · [回归原件](acceptance/v31/regression-final/frozen-comparison.json)
- 历史 child 测试覆盖缺少开始通知、普通 fork、跨项目、越权写和租约到期；简报历史重放逐一复核了 338 份摘要，最大预载 4,722 字节。这两类证据与 P2 的 Decision Lease 和 Graph head 新鲜度要各自验收。[child 官方线路](acceptance/v31/b2/child-recovery/final-official-lost-start.json) · [简报重放](acceptance/v31/brief-replay.json)
- GX 服务端是业务状态写入者；人类输入回执、Decision proposal、Task 产物、后端 Job 回执和模型笔记具有不同权限。已审阅插件的 Python 代码在 GX 进程中执行，命名空间、摘要和导入检查支持来源约束，无法证明任意 Python 不会发进程或不会死循环。给第三方作者的接口须限制在已声明的可观察步骤；禁止裸 Job/无界循环的验收须写清测试方法和可信代码前提。[导入与执行](gx-h/gx_core/plugins/imports.py#L33) · [插件运行上下文](gx-h/gx_core/plugins/runtime_context.py#L96)

## 对接口设计有直接影响的反例

1. 外部执行必须区分 Task Attempt、工作流节点的计算 Attempt 和后端 Job。旧工作者一次租约可创建两个 Job。历史真实 grid 项目的三个工作流 Job 都有执行 manifest，但其中的 Attempt 可以是 null。旧 Job 在磁盘上用独占文件和 PID/启动时刻取得的本机恢复证据，应保留给原路径；在远程队列接受后丢回执，GX 需要一个提交前已知的名字。现有环境注册只要求可选的 container_digest 是字符串。现有 WorkflowManifest 的 checkpoint_policy 描述引擎游标，CapabilityManifest 尚无求解器 checkpoint 合同。[原生 Job 记录](acceptance/v21/compute/official.json) · [三项实际计算](acceptance/v31/quality/final-live/evidence.json) · [本机丢回执证据](acceptance/runs/jobs/lost-launch-ack.json) · [环境字段](gx-h/gx_core/capabilities/environments.py#L151) · [两种 manifest](gx-h/contracts/bundles.json#L529)
2. 当前引导允许 hook 接触可变 spec/初始条目，Intent 更新路径也将条目传给 validate_intent 后再计算 diff。仅添加 Goal 字段不会确立人类所有权。旧维护入口存在通过既有治理规则批额度的路径，发行默认规则为空；不能断言现网模型能无条件加预算。原生 thread goal 控制一次工作线程的生命周期。两者都需要和人类科学 Goal 分开。[引导](gx-h/gx_core/p0/cli.py#L138) · [条目校验](gx-h/gx_core/p0/intent.py#L464) · [批准规则](gx-h/config/approvals.json#L5) · [原生 goal](gx-h/gx_core/p0/terminal_products.py#L199)
3. 可见的工具目录由 GX 裁剪，但写权限由服务端按调用身份决定。Run 级 coordinator 只有一把租约；Task 的 lease 指向真实 task，主线程并没有以此领取 Decision Task。已有保留工作的等待记录需要先有 Root。Codex 尚未启动时的分支等待必须能先持久化。旧托管协调入口、一次 429 退避和多 Run 冷重放都不能证明 P2 单 Run 多分支断连自治。[协调与 Task 租约表](gx-h/contracts/db_schema.sql#L85) · [Root 定义](gx-h/gx_core/p0/roots.sql#L1) · [已有等待](gx-h/gx_core/p0/waits.sql#L2) · [其他入口](gx-h/gx_core/project_continuation.py#L612)
