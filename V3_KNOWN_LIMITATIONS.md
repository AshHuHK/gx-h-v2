# GX-H v3 P0/P1 快照的适用范围

本文件对应产品 `2476f2bdd74b53e3833ce35d08287120e4fdf0c8`，附件 SHA-256 为 `d836e34d131cac4dfc3616950255d5fd4cd3c792da50dbf5c83664cf48ba7295`。离线安装、插件列表与核验、显式选择网格插件的新项目启动，以及读取旧项目状态已有实测。P1 性能结果和 P2/P3 签收分别记录。[部署报告](V3_DEPLOYMENT_REPORT.md) · [当前验收状态](V3_ACCEPTANCE_REPORT.md)

| 范围 | 当前限制及实际影响 |
|---|---|
| 性能 | 两小时真实模型比较的两侧各完成 26 个 Run 和 52 个 CPU Job。P1 的派发与回调原始 p95 超出预先设定的复核线，完成 Run 吞吐相差约 0.34%。部署通过与性能签收独立记录，复核状态以阶段报告为准。[两小时比较](acceptance/v3/performance/COMPARISON.md) |
| 插件与历史状态 | 此附件实际装入 `org.gx.science-grid@2.0.0`。新项目顶层的 `plugins` 选择精确版本，历史项目使用原有工作流。P2 的第二个独立插件及移除隔离，P3 的双版本同进程运行、组合摘要、缺包与精确回退，均另有门槛。[阶段验收矩阵](V3_ACCEPTANCE_PLAN.md) |
| 操作系统与离线依赖 | 附件面向 Linux x86_64，Python wheels 为 CPython 3.11。Node 22.17.0、npm 10.9.2 是本次实测组合。通用 Artifact 分析 Job 另需系统 libseccomp；可选 DSH SDK 与独立 runtime 由单独安装提供。本次离线部署仅验证随包 Linux 依赖与本地 CPU 数值路径。[发布依赖](RELEASE.md) · [继承的运行限制](V21_KNOWN_LIMITATIONS.md) |
| 官方模型与私有状态 | 安装、摘要核验、只读状态和本报告的带键启动均可离线。使用真实模型需要专用 Codex 状态、操作员登录、可达的 Clash 代理和对应的资源预算。归档内只有固定的软件及随版本保存的验收材料；项目数据及登录状态由操作员另存。[公开启动](V3_DEPLOYMENT_REPORT.md) |
| 回退 | 完整软件与数据回退演练针对 v2.1 暂停快照和较早的 P1 提交；`2476f2b` 的同版本证据覆盖冷恢复至完成，附件本身又执行了只读打开。升级后新增的外部工作和费用需要单独对账。[逐项回退证据](V3_ROLLBACK_REPORT.md) |
