# ADR-02　插件配方、工作流与持久计算

**Phase 1 待审。** 插件提供科学算法、图配方和方法声明。GX 将选中的配方实例化为 Run 的 GraphVersion，负责推进节点、提交计算、结算和恢复。

## 配方进入现有引擎

**existing**　公开入口先定位发行版工作流，再绑定插件。已安装的 grid@3 插件注册图引用、工具和能力，发行包构建五个节点并绑定实现。复用包摘要、依赖闭包、工具 JSON Schema、包内 JSON 能力清单、WorkflowRegistry 和 Engine。[启动入口](../../gx-h/gx_core/p0/cli.py#L121) · [grid 插件](../../gx-h/plugins/installed/org.gx.science-grid/3.0.0/plugin.py#L45) · [节点绑定](../../gx-h/workflows/science_grid_v3/__init__.py#L14) · [能力清单](../../gx-h/gx_core/capabilities/shipped.py#L92)

**extend**　增加包内 Recipe、具名 node handler 和 evaluator 注册。GX 从已核验的包字节解析 Recipe，将节点和依赖映射到现有的 `name/deps/work`，校验绑定完整性、无环依赖、输入输出类型、有界展开和资源上限。节点只接收本次执行获准的输入、参数和 Artifact，通过具名操作返回产物或请求已声明的计算。GX 将请求转给工作流 Job 入口。现有 `ctx.jobs.propose` 继续服务于获准的工作者回调。Evaluator 返回有来源的 Measurement，GX 按 PI 确认的 Goal Contract 判定。算法内部的方法阈值随方法版本冻结。[注册接口](../../gx-h/gx_core/plugins/author_api.py#L128) · [图构建](../../gx-h/gx_core/orchestration/loader.py#L37) · [工作者调用](../../gx-h/gx_core/plugins/runtime_context.py#L125) · [工作流 Job](../../gx-h/gx_core/jobs/workflow.py#L10)

**new**　`graph_versions` 持久化不可变实例。GX 展开默认值后冻结有效参数、seed、Goal 引用与摘要、资源、分支依赖、monitor/stop 规则、配方及插件闭包摘要、handler/capability/evaluator 摘要，并记录父版本、作者、DecisionRecord 和校验摘要。仅在期望图头及决策授权仍有效时提交 amendment。冷恢复读取原实例。同一个 Engine 将按分支依赖选择就绪节点，复用无关分支的结果。旧 Run 按其原工作流版本和摘要恢复。[原图摘要](../../gx-h/gx_core/orchestration/engine.py#L568) · [原就绪顺序](../../gx-h/gx_core/orchestration/engine.py#L1380) · [旧组合恢复](../../gx-h/gx_core/plugins/runtime.py#L101)

## Attempt、Job 与 checkpoint

**new / extend**　`node_attempts` 记录节点执行。对于外部计算，GX 先持久化 Attempt 和提交意图，绑定 Run、图版本、节点、GX 重算的 `exec_key`、方法及输入摘要、资源和幂等键。一次 Task Attempt 可发起多项有界计算，每个 fan-out slot 先固化为独立计算 Attempt，后端稳定名由这个 Attempt ID 派生，例如 `gx-node-<attempt_id>`。原有 outbox 向 JobService 交付请求，后者在启动后端前保存 Job、预算预留和 admission manifest。收到终态后沿用原有核算、Artifact 和事件。P2 仅新增 `graph_versions` 与 `node_attempts` 两张表。[原 Job 准入事务](../../gx-h/gx_core/jobs/service.py#L674) · [执行清单](../../gx-h/gx_core/p0/manifest.py#L103) · [终态与 outbox](../../gx-h/gx_core/jobs/service.py#L1446)

若后端已接受、GX 尚未收到回执，恢复时按后端实例及稳定名字查询，核对请求身份再接回同一 Job。只有后端能证明尚未提交，或对同一身份保证幂等提交，才允许重送。身份冲突、不可查询或暂不可见时保留 `OUTCOME_UNKNOWN`。已结算结果及费用在重放时复用。[已有未知结果处理](../../gx-h/gx_core/jobs/service.py#L1515)

**extend**　为新 capability 增加带版本的运行时声明，包装既有 JSON bundle 并将声明纳入能力摘要和准入回执。旧格式沿用原校验和摘要。声明列出后端、可选的精确镜像摘要及 `checkpoint_support`、读写格式和兼容条件。遇到不支持的必需特性时拒绝执行。Engine 的 checkpoint 保存图进度。求解器的 checkpoint 是带来源和内容摘要的 Artifact。后端在原 Attempt 声明的 checkpoint 槽位写入可恢复快照及其摘要，由 GX 验证并登记为单独的 checkpoint Artifact；允许已中断的计算留下有效快照，原有失败诊断仍按 UNVERIFIED 留存。续算前确认父执行已终止或已由后端有效隔离，若状态未知先对账。验证原运行、父 Attempt、算法格式、输入和状态兼容性及实际字节。通过后创建带 `parent_attempt_id` 和 checkpoint 引用的新 Attempt。验证失败时记录原因，保留原 Attempt。[既有严格清单](../../gx-h/gx_core/capabilities/manifest.py#L206) · [引擎 checkpoint](../../gx-h/contracts/db_schema.sql#L1013)

## 待验证的边界

- **A07** 在已审阅并锁定摘要的插件上，合同用例应拒绝无界的展开声明、调用私有调度器的操作声明和未登记的 Job 请求，并核对测试后端记录的每次提交均能追溯到 Attempt 和 Job。Python 在 GX 进程中执行。任意代码的终止性及进程外副作用超出导入检查的证明范围。[导入执行](../../gx-h/gx_core/plugins/imports.py#L33)
- **A21** 对使用容器的 P2 Job，后端在启动前核实实际镜像摘要与已批准能力中的摘要一致。未授权或只有可变标签的请求应产生拒绝回执，后端启动数为零。当前环境字段只校验字符串类型。[环境登记](../../gx-h/gx_core/capabilities/environments.py#L151)
- **deferred**　热传导独立插件是后续验收案例。用冷启动运行包内 FTCS、ADI 和测量节点，保存数值场并与独立解析解比较，才可记录本机数值计算证据。LocalSchedulerSimulator 的接受后崩溃和 checkpoint 故障用例单独标为 `SCRIPTED`。生产 Slurm 另行验证。
