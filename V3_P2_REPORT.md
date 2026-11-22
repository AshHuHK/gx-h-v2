# P2 独立科研插件

**待 P1 性能门槛签收。** 当前版本已经用网格插件打通公开 Root 工具、Capability Registry、Job 和 Artifact。P2 要求另一个独立安装的插件通过同一套公开 API 接入，并验证安装该包时的 Kernel 源码差异为零。

预定用例使用 `module_collaboration@1`，输入已有的四个等待时间 `[6, 10, 8, 8]` 分钟。独立插件应由 Root 提交一次 CPU Job，核对数量 4、最小值 6、最大值 10 和均值 8，再验证恢复后能重用原 Job 与输出引用。此用例尚无 P2 执行回执。[验收矩阵](GX_H_V3_Lean_Implementation_Package/acceptance/V3_ACCEPTANCE_MATRIX.md) · [当前阶段签收](V3_ACCEPTANCE_REPORT.md)
