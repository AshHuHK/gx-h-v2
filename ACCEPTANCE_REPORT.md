# P0 部署与科研闭环验收

本次修复按等待与恢复、多轮科研输入、受限真实领域闭环、后续扩展分批记录。[本次实施与验收](ACC_1_IMPLEMENTATION.md) · [采纳的修复计划](FIX_PLAN_ACC_1.md)

## 修复前的部署记录

下表保留原提交的部署记录。其中预算争用由脚本手工推进，三轮项目使用合成数据；该版本在生产 Driver 上复现了六项缺陷。后续签收请使用上方的分批报告。[修前诊断](acceptance/runs/acc-1-before-fix/observations.json) · [启动和登录](DEPLOYMENT.md) · [上游来源和完整性](audit/CODEX_SOURCE.md)

| 验收 | 实际结果与适用范围 |
|---|---|
| 三轮参考闭环 | 公开入口启动、暂停并恢复同一项目。两条初始路线、三轮、4 个 CPU Job、6 个独立 worker Root。复核 58 份产物，观测值按原始坐标与冻结公式重算。7 个原生 turn、40 个本地模型请求、1,400 个脚本 token 与 GX 账本一致。终态重开新增调用、作业、提交均为 0。[三轮完整记录](acceptance/runs/e2e-grid/summary.json) |
| 真模型科学决策 | 现有 ChatGPT OAuth 下，`gpt-6.2-astra/xhigh` 的真实审查与决策 Root 完成两轮、3 个 CPU Job、4 项已接受的任务。第一轮淘汰负向路线；第二轮验证临界点内外四个样本，2/4 达标，按原预测停止。首次不合格决策触发独立的新尝试。该项目位于 `var/oauth-acceptance`。这个早期运行有两笔最终用量回执缺失。[实际决策、预算与线程](acceptance/runs/oauth-project/summary.json) |
| 人类输入与项级影响 | 13 个官方主线程 turn 验证补充、提案、显式变更、拒绝伪造、重复消息和三类二次确认；主线程的真实回执与受信客户端动作共同决定资格。收紧条件时仅处理相关对象。GPU 无实耗，模型为受控本地回答。[输入与正式版本](acceptance/runs/intent-cli-confirmations/summary.json) · [事务顺序](acceptance/runs/intent-races/) |
| 变更评估与研究目标 | 改阈值用旧 Job 的原始观测重新评估并重新派独立审查。替换目标保留原 run、原成本、旧 Job 和评审，新的、经主对话登记的 proposal 在同一 run 执行 1 个新 Job，旧 lease 拒写，重开零新增。未提供匹配目标和评估版本的新方案时维持 PAUSED。[评估替换](acceptance/runs/science/) · [目标切换](acceptance/runs/replan-grid/summary.json) |
| 执行清单和证据 | 对照 CPU 后端收据、原始/发布文件 SHA、执行参数和冻结 Evaluation。实测两种内容替换均被拒作当前 Evidence；有效负面测量与测量失败分别结算。三轮决策覆盖 continue、prune、pivot、stop。[Job 清单和约束](acceptance/runs/jobs/) · [四类结算](acceptance/runs/science/test_four_classes_and_reviewed_evidence.json) |
| 预算与恢复 | 两个 Root 实际争用预留，第一笔 100 token 扣除 35 后，第二笔最多预留 65；最终结算 70、余额无悬挂预留。强杀情形中 GX 自动恢复受影响的 Thread/Attempt；完整结算后恢复零新增。[预算](acceptance/runs/e2e-contention/evidence.json) · [故障验收](FAILURE_INJECTION_REPORT.md) |
| 上游协议 | 17 个协议和故障场景各实际独立运行两遍。包括 Goal、主线程禁 Goal、原生 child、steer/interrupt、动态工具、compaction/fork/resume 和 provider 不匹配。真实 OAuth 单独验证 `xhigh` 推理；`max/ultra` 核对账户模型目录和真实开启的线程配置。[协议证据](audit/NATIVE_ACCEPTANCE.md) · [OAuth 验证](audit/OAUTH_EFFORT.md) |
| 回归 | 源树快照全量 Python 210/210，Node 2/2，生成器通过。起始基线的两个 crash 测试修正了夹具身份，现实际触发 SIGKILL。快照后 CLI/driver 与同项目换目标的少量改动由定向回归和正式公开入口再验。[快照、命令和完整日志](acceptance/runs/final-regression/) · [进程观测](acceptance/runs/baseline-fix/observations.json) |

完整验收矩阵逐项给出 37 个 ID 的实际执行边界、输入与产物。模型夹具和实际 OAuth 的口径分别保留。物理 GPU、商业第三方 provider 的真实模型质量、上游逐 HTTP 请求绝对消费上限等剩余范围见限制页。[验收矩阵](acceptance/e2e_matrix.md) · [已知限制](KNOWN_LIMITATIONS.md)

最后的发布树另以公开入口复跑参考项目，三轮、四个 Job 和 1,400 个脚本 token 对账一致。主 Root 也通过实际动态工具读到了自己的正式输入结果和版本 1 目标。首次测试导出器试图查询一个不存在的日志列，修正查询后复测通过。最新的换目标与部署专项 7/7。[发布复跑](acceptance/runs/release-verification/grid/summary.json) · [主对话实测](acceptance/runs/release-verification/main-final/summary.json) · [专项回归](acceptance/runs/final-regression/final-focused.log)
