# P0 部署前源码审计

本次目标是本目录的独立部署。输入为 gx-h-2 发布提交 `4ff89fa93b2f0f9c33fc59a45ad09ddfa17c59b7` 的 `gx-h` 子树 `e631154299192ccbf32111550c223f7642161a2e`。基线快照有 286 个跟踪文件。历史项目继续使用原发布。

| 对象 | 已核对的调用和边界 |
|---|---|
| GX 持久化 | `gx-h/gx_core/persistence/store.py:123` 用事务创建 DDL 并盖章，`:344` 为实际打开入口。`:1246` 的 Outbox 与变更可以共享同一 SQLite 写事务。 |
| 运行与调度 | `gx-h/gx_core/orchestration/service.py:467` 在 GX 创建 Run、Objective 和预算，随后直接启动 workflow。P0 必须在首次 Job 之前完成初始 Intent。 reactor.py:188 用已有 checkpoint 推进工序。 |
| Job、租约及收据 | jobs/grid_workflow.py:10 用稳定 operation ID 找回原请求。jobs/service.py:659 在写事务内检查租约和预算，:1014 在启动进程前提交提交意图，:1453 以后按真实收据对账。 |
| 科研结果 | workflows/science_grid/nodes.py:94 的 evaluator 是可选项，:193 校验产物引用，两边都缺失时也可能相等。已有 evidence、hypotheses、evaluations 表仍需写入和来源匹配。 |
| Codex 接入 | hostobs/app_server_client.py:353 的同步双向客户端能分派服务端请求和通知。existing science_cycle.py:96 的 OfflinePeer 伪造协议事件，适合单元测试。原生 Root 恢复需要真实 App Server 验证。 |
| 人类来源 | 官方协议 turn.rs 中 clientUserMessageId 是可选的客户端字段；turn/start 本身也可由软件发送。凭这个字段或消息正文无法判定人类授权。 |
| 上游锁定 | GitHub annotated tag `a41fe5b443652cbdbee57717fbb9fd78d8d8b32a` 指向 `d32063f96c2dff1dc96af839b3952abb6acfeda4`。本机 CLI 实报 0.169.0。 |

完整表、读写点、源码/测试清单见 [表索引](audit/gx_source_tables.json) 与 [源文件和测试索引](audit/gx_source_files_tests.json)。上述代码行为来自静态检查。实测包括 [官方 tag](acceptance/runs/schema/upstream-tag.txt)、[发布包和 CLI](acceptance/runs/schema/cli-provenance.json)、[170 项干净树基线](acceptance/runs/baseline-clean/unittest.log)。账户访问、模型等级、故障恢复和端到端结果由各自验收报告记录。
