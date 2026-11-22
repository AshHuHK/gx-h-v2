# v2.1 已知限制

- 本次部署为 Linux x86_64 staging。限时连续验收按本轮两小时以内的要求执行。长程报告给出真实起止时间、原生 turn、Job、重启和测量结果。本轮没有 8 小时或 24 小时生产运行数据。[长程记录](V21_LONG_RUN_REPORT.md)
- 官方 Codex 0.169.0 使用本地可控 Responses 服务验证调度、限流、子代理、预算和恢复。2RE9 输入、两条路线的 CPU 计算和独立数值复算均实际执行。另以本机现有 ChatGPT OAuth 向 OpenAI 发出一次真实连通性请求，模型为 gpt-6.2-astra，effort 为 xhigh，用量 16,206 token。完整多轮研究中的模型判断使用环回服务。真实模型自主选择研究路线、多供应商在线行为及货币费用仍需在目标部署验证。[研究产物](acceptance/v21/review-job/README.md)
- 硬 token 上限约束新计算准入，并在官方用量达到阈值时中断已开 turn。上游可以在 GX 收到用量通知前产生额外消费。在 100-token 用例中实际收到 103。保留真实费用、已发中断和终态。需要供应商强制限制单次响应才可保证更严的物理上限。[双 Root 记录](acceptance/v21/protocol/budget.json)
- 终态已知、用量不全时可按宽限估计释放预留，thread 后续累计可更正总账。上游未提供可区分的逐 turn 用量时将显示不确定归属；投递是否被接收仍不明确时，继续核对原线程。离线本地操作可检查待办和计量。[账本验收](V21_ACCEPTANCE_REPORT.md)
- 通用分析 Job 接受文档列出的 Python 表达式与 JSON 输入，配置限定 CPU、内存、磁盘写入、输出与运行时间。实际执行依赖 Linux、procfs 和 libseccomp；首次执行可明确报告所缺依赖。复杂科学方法应作为可固定版本、可审查的工作流或适配器提供。[能力契约](gx-h/adapters/local/artifact_analysis/README.md)
- 新研究示例默认使用 `tl1a_geometry@3` 的异步复算。旧 `@1` 和 `@2` 保持各自原方法与同步行为，以重现历史研究；已完成的研究沿用其原判据和结果。[版本与复算](acceptance/v21/review-job/README.md)
- 固定依赖含可选的 Cordis 插件测试。DSH 完整独立 Runtime 的 Python SDK/Runtime wheel 对不在此发行依赖中；其四个可选测试在基线与候选均有明确跳过原因。GPU/Slurm 和外部商业 Provider 未参加本轮验收。[部署环境](V21_DEPLOYMENT_REPORT.md)
- 旧库迁移和回退都在 v2 一致快照的 staging 副本实测。旧库与新库在切换后产生不同写入时，应保存两份状态并按迁移报告选择恢复点。回退普通数据库副本不可能撤销上游已发生的计算或消费。[回退演练](V21_ROLLBACK_REPORT.md)
- 共享盘上的官方客户端在部分测试退出时记录了清理旧临时目录的跨文件系统警告。相关进程正常退出，原生收据与重复恢复检查均通过。[原始退出日志](acceptance/v21/final-a05dcef/supplement/runner.log)
