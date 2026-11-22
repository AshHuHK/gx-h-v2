# P1 网格科研插件与派发

操作员在项目 JSON 的顶层加上 `"plugins": ["org.gx.science-grid@2.0.0"]`，选择该项目的插件。公开入口 `bin/gx-p0 start --state DIR --run-key KEY --spec FILE` 将所选包、工作流和能力回执随 Run 创建一同提交。可以用 `bin/gx-p0 status --state DIR --run-id RID` 查看记录。

## 科研调用路径

| 所有者 | 本次实现 |
|---|---|
| 插件包 | `register(ctx)` 引用原有 `science_grid@2`，提供项目启动输入、科学任务上下文和可供 Root 调用的 `gx_grid_measurement`。工具按当前任务的路线 ID 读取真实 Job、原始观测与冻结判据，返回观察数量、最大值和通过阈值的数量。[插件实现](gx-h/plugins/installed/org.gx.science-grid/2.0.0/plugin.py) |
| Workflow Registry | Run 继续使用原图、评审产物校验与完成判定。原图 SHA-256 为 `4f922e187a641e36c679e2a78879ce81dec0b687342bd14779293c7771b4f467`。各 Run 的额外钩子与工具由所选包构造。[Run 绑定](gx-h/gx_core/plugins/runtime.py) · [对照图谱](V3_MIGRATION_MAP.md) |
| Capability Registry / Job | 插件声明 `fixture-grid@1`。能力注册和准入仍使用原方法的清单及治理状态；可移植摘要 `b69193d4546b42225261484872718eb475478c5836377de6cacd6b8c38b9f12f` 和当前工作区实际产物引用都写入回执。执行、记账、取消和 Artifact 保留各自原有所有者。[能力附件](gx-h/gx_core/capabilities/shipped.py) |
| Root 与派发 | 对新任务依次检查依赖、Provider、团队和调度容量，再执行 `prepare_dispatch`。需要重新评分或补审时，释放领取事务进行准备，重新确认任务和科学输入后才签发租约。受到阻塞的任务按任务和尝试退避。[领取逻辑](gx-h/gx_core/p0/roots.py) · [数值工作流的准备与提交](gx-h/workflows/compat_v20/grid_dispatch.py) |
| 通用科研记录 | Driver、Impact 和 Root 通过共享科研记录访问已有的人类来源、已接受的提交、尝试和决策。兼容回放仍保留最初固定的图和计算代码。[科研记录](gx-h/gx_core/research/records.py) · [新旧投影实测](acceptance/v3/p1/grid-projection-parity.json) |

## 部署和回退

只在独立 v3 安装和测试数据副本中运行。旧 v2.1 的项目不携带插件选择，打开时沿用原工作流。已有 v3 项目续跑需要原插件摘要、方法回执和工作流版本。恢复时可以对照公开状态中的 `plugin_binding`。原 v2.1 数据副本的回退步骤见 [冷恢复操作](acceptance/v3/staging/README.md)。
