# GX-H v3.2 · Phase 1.5 决策增补

本轮基线是 `v3.2-p2` 的 `51e0e13583d95e9cad1386e1d4c77324977e97ea`。本表对照交付包中的 11 项决策清单，记录架构约束及实现阶段。2A 运行验收另以同一 Run 的节点、Job、事件、用量和结算记录为据。[交付包](GX_H_Phase1_5_Phase2A_Package.zip) · [总决策](V32_ADR_DECISIONS.md) · [2A 源码路径](PHASE_2A_SOURCE_MAP.md)

| 项 | 决定 | 归属与依据 |
|---|---|---|
| 1 · 通用协议 | P2 的 Terminal Submission、Task Brief/Artifact Preloading、Role Tool Catalog 和 Product/Evidence 交付须能依靠会话、任务及插件声明工作，不依赖 `science_grid@3` 特判。2A 回归旧 Grid，2D 完成通用实现。 | [ADR-05](p2/ADRs/ADR-05.md) · [ADR-06](p2/ADRs/ADR-06.md) · [ADR-03](p2/ADRs/ADR-03.md) |
| 2 · 插件执行 | 当前受信 Python 的进程内导入不提供沙箱。未来第三方 Node Handler 须在 GX 持久循环和 SQLite Writer 之外运行。普通节点使用有界 Worker Process，重 CPU/GPU/HPC 使用既有 Job Runtime。Worker Runtime 留待 2A 之后。 | [ADR-02](p2/ADRs/ADR-02.md) |
| 3 · 后端能力 | 2C 要求后端分别声明提交幂等性、按稳定身份查询、权威未提交证明、取消和 checkpoint（格式与兼容性）。JobService 依据明确支持的能力决定能执行的操作。 | [ADR-02](p2/ADRs/ADR-02.md) |
| 4 · 三种身份 | `attempt_id` 标识 GX 节点执行，`operation_id` 标识外部提交操作并跨该操作的重试保留，`scheduler_job_id` 是后端实际作业句柄。先落意图，再投递并关联句柄。生产 operation_id 后端留至 2C。 | [ADR-02](p2/ADRs/ADR-02.md) · [ADR-04](p2/ADRs/ADR-04.md) |
| 5 · 结果未知 | 未知或不可见时沿原 Job 保留 `OUTCOME_UNKNOWN`、预留及证据，退避后查询。2C 的策略窗口届满后交 operator 凭权威证据绑定已有作业、确认尚未提交或保留未决。盲目重投会重复执行，单次查无或超时不能授权重送。2A 只验证已有 Job 暂时不可查询时延后对账。 | [ADR-02](p2/ADRs/ADR-02.md) |
| 6 · SQLite Writer | 在锁外准备图/Schema、依赖和影响闭包、大对象解析、哈希与文件字节。事务内复核状态、版本、Lease、身份授权、预算与幂等后，才条件登记引用、状态、回执及 outbox。依据过期须在锁外重新准备。 | [ADR-04](p2/ADRs/ADR-04.md) · [ADR-03](p2/ADRs/ADR-03.md) |
| 7 · Decision Lease | 2B 复用既有 Task/Lease。Run 与待决 scope 唯一绑定有效持有人/session、适用的 base 版本、到期时间。仅有效 owner 可条件续期，接管先撤销，提交时复核。2A 用原 checkpoint 表示决策等待。 | [ADR-01](p2/ADRs/ADR-01.md) · [ADR-06](p2/ADRs/ADR-06.md) |
| 8 · 单头 CAS | 2B 每个 Run 只有一个 head。以 base 版本和 SHA 条件提交，冲突方读新 head 后显式重提。记录冲突率、重提次数、额外 token/耗时及实际 scope 重叠。多头与自动合并均不在此设计内。 | [ADR-04](p2/ADRs/ADR-04.md) |
| 9 · 初始化 | 首版 GraphVersion 前须可恢复 Goal、硬预算、精确插件组合及初始化授权。优先使用现有 Run/Intent/预算/插件组合和 operator 记录。无须注册虚构科研 Workflow 或新增 Run 状态机。 | [ADR-01](p2/ADRs/ADR-01.md) · [ADR-03](p2/ADRs/ADR-03.md) |
| 10 · 2A 边界 | 仅调整原静态 Engine/Driver 的就绪节点选择、局部等待、Job 对账、确定性推进、用量与恢复。动态图/GraphVersion/CAS/Amendment、`graph_versions`/`node_attempts`、plan_key/exec_key/seed 复用、完整 Decision Lease、远程 Slurm、生产 operation_id 后端、求解器 checkpoint 续算、Plugin Node Worker Runtime、独立科研插件及 Research Intelligence 均留到后续。 | [本轮源图](PHASE_2A_SOURCE_MAP.md) · [ADR-01](p2/ADRs/ADR-01.md) · [ADR-02](p2/ADRs/ADR-02.md) · [ADR-04](p2/ADRs/ADR-04.md) |
| 11 · 六份 ADR 一致性 | 01 的 lease 与 04 的 head 在同一提交事务核验。02 的节点/后端仅向 GX 返回结果，03 的 Goal 及计算裁决仍由原所有者持久化。05 的终态交付和 06 的上下文均使用同一 Task/Attempt 和获准引用。离线等待保留这些身份和未结账目。 | [六份 ADR 总表](V32_ADR_DECISIONS.md) |

2B 验证动态图、决策租约和竞争提交。2C 验证外部提交的恢复和 checkpoint 血缘。2D 验证非 Grid 的通用研究协议。Phase 3 再以独立科学插件取得领域结果。2A 的静态分支推进、重复执行次数及 v3.1 回归须用本轮实测记录分别判定。
