# v3.1 提交协议与科研产物

## A 与 C 的事务边界

在 `science_grid@3` 的原生工作线程上，GX 在同一事务里保存产物裁决、工具调用回执与终止原因。提交成功后，工具回调返回 `NO_TOOL_RESPONSE`。外层循环处理 goal 和对原 turn 的中断，并从官方通知或历史记录核对结果。关闭连接、ACK 延迟或 GX 重启均以已提交的科研结果为准。Codex 自己留下的“调用取消”记录和 GX 的科学接受或拒绝可以同时出现在相应日志里。[事务与恢复](gx-h/gx_core/p0/terminal_products.py) · [原生投递入口](gx-h/gx_core/p0/roots.py)

结构错误沿用正常工具错误回复。GX 先回滚原事务，再用独立小事务按拥有本次 Attempt 的 Root 线程和官方 call ID 保存错误、请求摘要、合同摘要和次数。重放同一调用会校验原请求摘要并复用原错误。一个新 call ID 在同一 Attempt 上可提交修正后的结果。默认到第 2 个不同的结构错误时结束该 Attempt；项目规格的 `max_product_structural_failures` 可以配置这个限额。达到限额保留诊断，后续任务处置沿用现有失败预算。[持久化定义](gx-h/gx_core/p0/zz_v31_terminal_products.sql)

## B 与确定性评估

审阅者提交 `findings_by_path`，键与冻结路径集完全相等，每条路径填写 `reason` 和 `risk`。有来源或正式需求需要回应时，合同单独列出本次应填写的判断。GX 绑定 proposal、evidence、observation、evaluation 与 Job 引用。版本内的纯函数先判断原始测量能否支持判别，再对照预先冻结的阈值求 `supported`。测量失败或证据不足时该值为 `null`。[逐路径合同与验证函数](gx-h/workflows/science_grid_v3/contract.py) · [从冻结记录绑定字段](gx-h/workflows/science_grid_v3/products.py)

项目可给纯数值路径声明 `claim_scope: finite_grid_threshold` 及相应的 `expected_reaches_threshold`。所有路径和证据都满足确定性评估条件时，GX 发布可追溯的评估产物，随后由 planner 决策。普通自由文本预测、机制解释、补充资料及尚有不确定性的测量进入独立审阅。评估记录能区分这两种来源。历史 `science_grid@2` 仍可从原工作流摘要恢复。[版本化图](gx-h/workflows/science_grid_v3/nodes.py) · [科研结算](gx-h/workflows/science_grid_v3/settlement.py)

## 从当前开发目录运行

以下规格沿用 13 波测试的西侧数值样例及单项目预算，适合带有锁定版运行时和现有 OAuth 登录的安装。命令使用独立的状态目录。调用 `run` 会使用模型配额。

```sh
bin/gx-p0 plugin verify org.gx.science-grid@3.0.0
bin/gx-p0 start --state var/v31-west --run-key west --spec acceptance/v31/waves/b1/spec-00-west.json
bin/gx-p0 run --state var/v31-west --timeout 600
bin/gx-p0 status --state var/v31-west
```

项目进行中可用 `bin/gx-p0 stop --state var/v31-west` 暂停，再用 `bin/gx-p0 resume --state var/v31-west --timeout 600` 在同一项目继续执行。

## 故障、兼容与集成验收

- 仅仅抑制工具回复有一个已复现的反例。Codex 的异步 `functions.exec` 可以在嵌套的提交仍挂起时先 yield，原对照由此在已提交事务后产生 1 次额外模型请求。B1 把 `gx.gx_submit_product` 放入官方直接调用专用命名空间，同时保留其他工具的原有调用方式。在锁定版宿主和实测所用模型目录上，嵌套查找无法取得该工具，直接提交可以正常接受或拒绝；即使延后中断 250 ms，受保护用例的额外请求仍为 0。[正反线路对照](acceptance/v31/terminal-code-mode.json)
- 在真实 SQLite COMMIT 后立即终止 GX 进程，接受及终局拒绝都保留原裁决、产物、回执、引用和费用。同一失败调用由两个写者并行重放只记一次；不同调用按各自身份计数。真实科学拒绝保留其提交关联的拒绝记录，GX 发出的中断没有另扣一次执行失败。[崩溃与并发回执](acceptance/v31/terminal-crash-concurrency.json)
- 模拟连续八个中断 ACK 丢失，再将回复逆序送达。已由原 turn 历史证实完成的请求会释放本地挂起槽位，后继任务仍能结束。另测同一 turn 连续两次中断命令丢失，恢复过程最终正常结束。[迟到回执与恢复](acceptance/v31/terminal-late-receipts.json)
- 对 32 路合同，Codex 将 19,354 字节的工具参数声明缩至 1,466 字节，普通工具输出只显示 20 份完整字段定义。GX 的冻结 schema 始终可按 Artifact 引用分页读取；官方宿主上 8 页重组的 19,259 字节与原件 SHA-256 相同。[大合同线路实测](acceptance/v31/schema-transport.json)
- 产品提交 `0db83d7` 的公开 CLI 实测覆盖确定性审阅、独立审阅、原 Attempt 修复三条路径，分别使用 6、13、14 个本地脚本模型响应。每次都运行了两个真实 CPU Job，核对数值、科学决策及终态重开。它们验证控制流和数据完整性，真实模型的效率另见实测波次。[纯数值路径](acceptance/v31/b1-public-deterministic.json) · [独立审阅](acceptance/v31/b1-public-review.json) · [结构修复](acceptance/v31/b1-public-repair.json)
- 从旧库冷恢复时，原 `science_grid@2` 摘要仍为 `4f922e187a641e36c679e2a78879ce81dec0b687342bd14779293c7771b4f467`；回到原项目完成 4 个 Job、6 份提交和 3 次决策。对真实历史备份另作两次可写打开，114 个既有业务表的原列、记录、外键和已有计量均核对通过。[冷恢复](acceptance/v31/b1-at2-cold.json) · [历史数据逐表比较](acceptance/v31/b1-at2-preservation.json)

## 同题 13 波真实 OAuth 结果

两组都完成 26 个项目、52 个真实 CPU Job、26 份独立审阅和 26 次规划决策。B1 沿用原来的自由文本预测，实际仍需独立 reviewer；每组 156 个数值观测均单独复算。B1 对应 104 个模型填写的逐路径 reason/risk 字段，未发现错误的支持方向、重复 Job、重复提交回执或错误来源归属。[逐项目数据与汇总](acceptance/v31/waves/b1/measured.json)

| 指标 | 原 v3 | v3.1 B1 |
|---|---:|---:|
| 首次 reviewer 提交被拒 | 26 / 26 | 0 / 26 |
| 原生 turn | 78 | 52 |
| 有完成回执的模型响应 | 364 | 157 |
| 可归因到终局提交后的完成响应 | 129 | 0 |
| GX 账本 token | 8,638,327 | 3,808,846 |
| 用量结算采用估算的 turn | 0 | 1 |
| 项目时长 p50 / p95 | 119.857 / 135.400 秒 | 54.374 / 76.675 秒 |
| 提交回调 p95 | 97.533 ms | 133.118 ms |

B1 的 157 份可直接对账的响应包含 106 READ、51 PRODUCE，共 3,673,577 个 provider 已报告 token。另有 1 次模型生成了直接提交调用，但缺少该响应的 completion 和用量；故可证实**至少 158 次模型调用**。52 个终局提交均找到 Codex 自己的取消工具记录。第 07 波东侧 reviewer 的两份完整响应合计 44,731 token，最后一份响应的实际用量未知。GX 按 180,000 的 turn 预留估算。两种合计之间的 135,269 是估算与已知下界之差，不能据此确定实际多计金额。[该 turn 的原始时序及账本](acceptance/v31/waves/b1/estimated-state-07.json)

已完成响应减少 56.9%，GX 入账 token 减少 55.9%。单项目时长改善，单次提交回调的尾延迟则上升，仍需保留为性能限制。13 波的启动节奏与旧测不同，部分时段还有单独的科研质量测试并行，因此本表不使用全程完成项目数除以日历时长作为提升倍数。[比较规则及结果](acceptance/v31/waves/comparison.json)

## 按实测故障修正终止和恢复

初版在 GX 提交后立即结束 GX 进程，等待旧 Codex 宿主退出，再开新宿主时，对仍携带 active goal 的线程直接执行 `thread/resume` 会额外启动一次未经预留的推理。最终实现先从未加载线程读取原 turn 与 goal，确认 goal 已完成。只有原调用、原始逐响应记录连续完整且确实还缺累计数时，才允许关闭 goals 后的计量恢复。显式关闭 rollout 诊断仍可完成公共协议对账。在真实跨进程重启测试中，7 份 provider 回执共 245 token，原账本只结清一次，额外模型请求和私有 rollout 读取均为零。[修复前复现](acceptance/v31/official-terminal-crash/b1-repeated.json) · [公共回执对账](acceptance/v31/official-terminal-crash/public-corrected.json)

另一个实测顺序是模型先发送直接工具调用，provider 的最终用量较晚到达。GX 在原生未回复工具上最多等待 1 秒，只为这一个活跃且尚缺完成事件的调用留出收尾时间。官方分段流中，延迟 300 ms 的完成事件在接受和终局拒绝两侧均按 70 token 精确结算；永远不发 completion 的对照在约 1 秒时中断，预留交回原有对账流程。已有完成回执的对照约 10 ms 即中断。五个对照都没有新增模型请求或 GX 正常工具回复。第 07 波原始估算保留。[官方流的五个对照](acceptance/v31/terminal-stream.json)

最终未打补丁的产品提交 `9cd6655` 使用 B3 简报后，按相同的断电位置重新实测。原 reviewer 的 3 个响应合计 105 token，冷恢复仅产生一条 105 token 的 provider 确认结算。额外模型请求、工具回复及 GX 私有 rollout 读取均为 0。后续 planner 的 3 个正常响应完成了同一项目，原两项 Job、租约和产物引用均保持一致。[最终产品冷恢复原始记录](acceptance/v31/official-terminal-crash/rc-9cd6655.json)

## 最终回归

冻结产品 `9cd6655` 的完整相关回归用时 289.231 秒，21 项命令全部成功。Python 共发现 446 项，442 通过、4 项既有可选 DeepSeek SDK/runtime 集成因隔离环境未安装该组件跳过。P0 专项 127/127，Node 2/2，另有 13 项原生专项和 17 项官方协议场景通过。此次受控测试使用官方 Codex 和本机脚本模型。已验证同一产品源码的 204 项终止协议、5 项代码模式与 5 项分段流对照。[完整回归](acceptance/v31/regression-final/summary.json) · [与冻结套件逐项比对](acceptance/v31/regression-final/frozen-comparison.json) · [终止实测索引](acceptance/v31/final-terminal/source.json)

最终版本的公开 CLI 上分别验证了独立审阅、同一 Attempt 修复及确定性评估，完成响应数分别为 6、7、3，均未发生额外终局响应。另从原 @2 数据库恢复并完成同一项目，114 张既有表的原列值保留。[最终 CLI 修复实测](acceptance/v31/final-public-repair.json) · [@2 冷恢复](acceptance/v31/final-at2-cold.json) · [逐表核验](acceptance/v31/final-at2-preservation.json)
