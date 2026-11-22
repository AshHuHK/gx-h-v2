# 运行边界

本次修复后的证据按运行时、模型和计算后端分别签收。当前真实领域用例是实验结构 2RE9 的坐标分析；更广泛的研究任务需要自己的数据、方法与结果验证。第二个生产厂商仍需要可用的兼容 Responses 端点与凭据。[本轮范围](ACC_1_IMPLEMENTATION.md)

缺少可归属的末次用量时，GX 保留原预留并显示原因。同线程后续付费 turn 等待对账。项目预算已耗尽时，使用同一预算的主对话也不能发起新的付费 turn，包括要求提高上限的消息。当前签收范围未包含免费的独立控制通道。现存 `science_grid@1` 项目按原版本继续；旧交付物中没有新来源实际审阅证据时，其来源处置显示待审。

原生 turn 里的上游请求由官方 Codex 执行。准入额度限制新 turn，已入场 turn 的实际 token 可能超过其预留。旧坐标研究的五笔全部结算至 1,618,296 token。扩大验收的规划 turn 则在旧驱动关闭连接时被中断，留有 162,230 token 的可归属小计和一笔 666,666 token 预留。原始计量中缺失末次用量，同任务的后继 Attempt 等待这笔账，独立评审可以使用其余已授权额度。官方接口没有返回这两项研究的美元费用。[旧预算实测](acceptance/runs/acc-1-research/oauth/budget-admission.json) · [冷对账证据](acceptance/runs/acc-1-research/oauth-v2/pending-usage-after-cold.json)

## 修复前的验收范围

- 随包验证的科研工作流是 `science_grid` 和真实的本机 CPU 计算。三轮验收使用确定性的环回模型，另一次使用 ChatGPT OAuth 的真实模型独立作出两轮决定。集群调度、GPU 硬件运行、真实 wet-lab 以及其他科学方法均需要各自的执行环境与验收。[实网项目](acceptance/runs/oauth-project/summary.json)
- 该官方协议暴露 thread、turn、Goal 和用量观测。一笔 GX 原生调用可包含多个上游 Responses 请求。GX 能限制可机检的能力、受控调用次数与 token 预留，结算采用真实观测。若要求强制限制每个上游 HTTP 请求或精确到实际生成的绝对 token 数，控制面明确返回 `unsupported`，不会提交这项新硬约束。一个故障 turn 缺失最终用量时，费用保留待对账。用完所有已授权模型预算后，新的付费主对话也需要可用额度。[资源测试](acceptance/runs/jobs/) · [模型计量](acceptance/runs/native/gx_child_meter.json)
- P0 通过本地受信客户端区分人类输入和模型工具内容。可访问同一操作员账户、数据库或受信输入端的其他进程位于相同信任边界。验收中程序驱动客户端的用例证明了事务与协议绑定；真人登录状态由官方账户接口确认。[授权验收](acceptance/runs/intent-cli-confirmations/summary.json)
- 项目验收会重新核对 Job、产物和清单。GX 本身无法在执行前拦截任意绕开 GX 的外部程序；P0 研究 Root 以只读方式启动，使用 GX 动态工具提交。故障探针特意开放了一个固定的 shell 命令来测量 steer 延迟。[协议验收](audit/NATIVE_ACCEPTANCE.md)
- OpenAI 及非 OpenAI compaction 的恢复协议使用官方二进制和本机受控模型响应验证。真实第三方厂商的远端 compaction 质量与费用未测。最终三轮验收无商业账单，OAuth 运行的实际美元价格未由服务端返回。
- 已完成的早期在线科研试跑保留原始账本。该试跑发生在最后一个 turn 的收费修复前，有两个 turn 的最终用量回执未入库。修复后的三轮验收核对每一笔脚本模型用量，原在线费用不作追溯改写。[历史在线快照](acceptance/runs/oauth-project/summary.json)
