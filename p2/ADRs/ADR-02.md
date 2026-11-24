# ADR-02　插件配方、工作流与持久计算

**Phase 1.5 决策。** 2A 通过静态 Workflow 和既有 JobService 验证已提交工作的持续对账。后续插件申明科学算法与配方，GX 负责准入、状态和结算。外部提交与 checkpoint 合同归 2C。[分期](../../V32_PHASE_1_5_DELTA.md)

## 配方进入现有引擎

**existing**　公开入口先定位发行版工作流，再绑定插件。已安装的 grid@3 插件注册图引用、工具和能力，发行包构建五个节点并绑定实现。复用包摘要、依赖闭包、工具 JSON Schema、包内 JSON 能力清单、WorkflowRegistry 和 Engine。[启动入口](../../gx-h/gx_core/p0/cli.py#L121) · [grid 插件](../../gx-h/plugins/installed/org.gx.science-grid/3.0.0/plugin.py#L45) · [节点绑定](../../gx-h/workflows/science_grid_v3/__init__.py#L14) · [能力清单](../../gx-h/gx_core/capabilities/shipped.py#L92)

**插件执行边界。** 既有受信 Python 插件会在进程内导入，这种信任不构成安全沙箱。未来第三方 Node Handler 在 GX 持久循环及 SQLite Writer 之外执行。普通节点放入有界 Worker Process，重 CPU/GPU/HPC 计算交原 Job Runtime 管理资源、取消和对账。只传获准的结构化输入、Artifact 和能力，GX 接收类型化结果或已声明的 Job 请求。Worker Runtime 留待 2A 之后实现。[现有导入](../../gx-h/gx_core/plugins/imports.py#L33) · [工作流 Job](../../gx-h/gx_core/jobs/workflow.py#L10)

**配方。** 未来 GX 从已核验的包解析 Recipe，将依赖映射到既有 `name/deps/work`，校验绑定、类型、有界展开及资源。角色工具、产物与测量按插件声明交付。Evaluator 返回有来源的 Measurement，GX 按 PI 的 Goal 判定。[注册接口](../../gx-h/gx_core/plugins/author_api.py#L128) · [图加载](../../gx-h/gx_core/orchestration/loader.py#L37) · [通用交付](ADR-05.md)

**new**　`graph_versions` 持久化不可变实例。GX 展开默认值后冻结有效参数、seed、Goal 引用与摘要、资源、分支依赖、monitor/stop 规则、配方及插件闭包摘要、handler/capability/evaluator 摘要，并记录父版本、作者、DecisionRecord 和校验摘要。仅在期望图头及决策授权仍有效时提交 amendment。冷恢复读取原实例。同一个 Engine 将按分支依赖选择就绪节点，复用无关分支的结果。旧 Run 按其原工作流版本和摘要恢复。[原图摘要](../../gx-h/gx_core/orchestration/engine.py#L568) · [原就绪顺序](../../gx-h/gx_core/orchestration/engine.py#L1380) · [旧组合恢复](../../gx-h/gx_core/plugins/runtime.py#L101)

## 2C 的外部操作与 checkpoint

**三种身份。** `attempt_id` 标识 GX 的一次节点执行。`operation_id` 是 GX 在联系后端前持久保存的一次外部提交操作标识，同一次操作的查询和安全重试沿用它。`scheduler_job_id` 是后端实际返回或经核验查询得到的 Job 句柄，提交结果未知时可以为空。三者用途独立。JobService 将本地 Job、后端实例、请求/输入摘要、预留和三种身份关联，节点可产生多项显式计算。先提交意图和 outbox，再由 JobService 按既有流程准入、冻结 manifest 和预留，之后才联系后端。[原 Job 准入](../../gx-h/gx_core/jobs/service.py#L674) · [执行清单](../../gx-h/gx_core/p0/manifest.py#L103)

**按后端能力对账。** 后端逐项声明是否支持同一 `operation_id` 幂等提交、按稳定操作身份查询、给出权威的未提交证明、取消、checkpoint（含读写格式及兼容条件）。缺失能力按不可用处理。已找到唯一且身份匹配的 Job 时绑定其 `scheduler_job_id` 并对账。只有后端保证同一操作幂等或给出权威的未提交证明时才可重送。查不到、暂时不可见、查询失败或匹配歧义时保留 `OUTCOME_UNKNOWN`、原操作、预留和证据，按策略退避重查。超过策略窗口后交已有 operator 入口，并附原始意图、查询记录与后端回执。operator 可凭权威记录绑定已有 Job、确认未提交后授权原操作重送，或保留未决状态继续查证。处置理由与证据归入同一 Job，超时本身不证明未提交。[已有未知结果处理](../../gx-h/gx_core/jobs/service.py#L1515) · [operator 入口](../../gx-h/gx_core/p0/operator.py#L20)

**extend**　为新 capability 增加带版本的运行时声明，包装既有 JSON bundle 并将声明纳入能力摘要和准入回执。旧格式沿用原校验和摘要。声明列出后端、可选的精确镜像摘要及 `checkpoint_support`、读写格式和兼容条件。遇到不支持的必需特性时拒绝执行。Engine 的 checkpoint 保存图进度。求解器的 checkpoint 是带来源和内容摘要的 Artifact。后端在原 Attempt 声明的 checkpoint 槽位写入可恢复快照及其摘要，由 GX 验证并登记为单独的 checkpoint Artifact；允许已中断的计算留下有效快照，原有失败诊断仍按 UNVERIFIED 留存。续算前确认父执行已终止或已由后端有效隔离，若状态未知先对账。验证原运行、父 Attempt、算法格式、输入和状态兼容性及实际字节。通过后创建带 `parent_attempt_id` 和 checkpoint 引用的新 Attempt。验证失败时记录原因，保留原 Attempt。[既有严格清单](../../gx-h/gx_core/capabilities/manifest.py#L206) · [引擎 checkpoint](../../gx-h/contracts/db_schema.sql#L1013)

## 待验证的边界

- **A07** 后续以独立插件验证有界展开、受支持的操作和已登记的 Job 每项均可追溯到原 Attempt。普通节点运行超时或退出时，GX 仍可对账其他工作和结算。Worker 的每次运行须有实际进程证据。[执行边界](../../V32_PHASE_1_5_DELTA.md)
- **A21** 对使用容器的 P2 Job，后端在启动前核实实际镜像摘要与已批准能力中的摘要一致。未授权或只有可变标签的请求应产生拒绝回执，后端启动数为零。当前环境字段只校验字符串类型。[环境登记](../../gx-h/gx_core/capabilities/environments.py#L151)
- **验收范围。** 2A 测已提交 Job 暂时无法查询时可延后对账且零重复提交。2C 再以可故障注入的后端验证受理后丢回执、未知结果、操作员处置和 checkpoint 血缘。生产 Slurm 需要自己的验证。Phase 3 的独立插件另以真实数值场和独立解析解验证 FTCS/ADI。模拟受理和 checkpoint 故障证据标为 `SCRIPTED`。
