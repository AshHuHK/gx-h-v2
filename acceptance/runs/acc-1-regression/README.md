# 61c70767 最终集成回归

从 `61c70767ca09eaa6775bb16f16c8a56ab0f71c04` 的 Git 对象直接导出代码、测试及已发布依赖，在独立临时目录安装带哈希锁定的 Python wheels 和四个 npm 离线归档。Python 为 3.11.9，Node 为 22.17.0。逐文件回查 Git，376 个产品、配置、脚本与测试文件全部一致，另核对 15 项原生验收脚本及发行依赖。产品文件集合 SHA-256 为 `4cb339b8800fca0c298bd4d1e559fb30013cc76551b6fd69d24c54dffe2705a9`。[提交比对](commit-equivalence.json) · [逐文件 SHA-256](files.json)

| 执行 | 通过 | 失败 / 错误 | 跳过 |
| --- | ---: | ---: | ---: |
| 全量 Python，277 项 | 273 | 0 / 0 | 4 |
| P0 专项 | 106 | 0 / 0 | 0 |
| 账本、租约及资源维度专项 | 46 | 0 / 0 | 0 |
| 领域与输入确认专项 | 4 | 0 / 0 | 0 |
| Node 插件宿主 | 2 | 0 / 0 | 0 |
| 角色/策略生成校验 | 1 | 0 / 0 | 0 |
| GX 与官方 Codex App Server 0.169.0 的回环复验 | 6 | 0 / 0 | 0 |
| 公开入口 pause-drain 与冷开 | 1 | 0 / 0 | 0 |

各专项与全量有重叠。四项跳过由测试标明是可选 DSH SDK、runtime 与其专属虚拟环境未安装。每项检查的命令、起止时间、状态和依赖摘要见 [summary.json](summary.json)。[全量日志](python-full.log) · [P0 日志](p0-focused.log) · [账本与资源日志](ledger-focused.log)

中断计量的回环服务在第二次 Responses 请求返回限流响应头及首个 SSE 事件后暂缓发送结果。官方 App Server 先后发出一个带 35 tokens 的精确响应回执和两个累计 35 的快照，随后正式记录 turn 中断。在 `316a8a3` 误结算后 warm/cold 均启动了新调用，最终版本始终保留原有 1000 tokens 与一次调用的预留。相同回环夹具中的两次后继尝试均未再发出模型请求。[修前证据](../acc-1-protocol/interrupted-prior-316a8a3.json) · [最终原生回执与 GX 账本](protocol/interrupted.json)

其余五项 GX 原生复验覆盖可信累计 70 的恢复、缺末次用量时拒绝后继、父结束后才到的 child 计费、正常及过期租约下的 wake ACK 崩溃恢复。公开 CLI 的暂停试验在计划者提交后仍有用量在途时结束，冷开后模型请求增量为零。[六项日志及账本](protocol/) · [暂停与冷开](pause-drain.json)

三个只观察官方线协议的样本复用自 `adceab86`。已比对探针源码、stdio 客户端及搭建回环的代码逐字节相同，官方 ELF 的 SHA-256 也相同。这三项观察使用确定性回环响应。实时 OAuth 科学运行由对应项目的独立记录覆盖。[官方线协议样本](adceab86/protocol/) · [最终源树差异与复用文件摘要](final-candidate-diff.json)
