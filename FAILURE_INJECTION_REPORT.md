# 故障注入与恢复

故障注入通过官方 App Server、实际 OS 子进程、SQLite 和 GX 包入口执行。下列恢复除显式测试操作者的 pause/resume、负责发出 SIGKILL 的注入器外，无人工完成任务或补写结果。耗时按各行说明的起止口径记录；没有独立计时器的条目保留原始时序，未估算恢复时长。

| 场景 | 预期与实际、重复副作用、耗时 |
|---|---|
| GX 在 Intent 事务内被 SIGKILL | 冻结版本、租约和 Outbox 一起回滚。重放同一人类回执后只有一份变更，第二次重放无额外处置。恢复样本约 0.22 s。[事务前后](acceptance/runs/intent-races/test_killed_change_writer_rolls_back_items_impact_and_outbox/recovery.json) |
| 活跃的 Codex App Server 被杀 | 第一个 reviewer 的请求已开始，应恢复同一 Thread/Attempt。原 CLI 自行重连，4.372 s 后整个三轮项目已停止。4 个 Job、6 个提交不重复。中断请求计入 41 次尝试中的 1 次，40 个有效应答。[进程和时序](acceptance/runs/e2e-kill/fault.json) · [最终成本](acceptance/runs/e2e-kill/summary.json) |
| 创建 Root 后 GX 未收到 thread ID | 按 Root 专属 cwd、来源、原服务进程和官方线程记录对账。仅对已证实没有接收过科研输入且旧进程已死的空 Root，在同一 attempt 内重建；原有创建痕迹可查。产生一次有效原生任务输入。[官方与 SQLite 双侧记录](acceptance/runs/native/gx_create_loss.json) |
| 成功投递后丢通知与响应 | 实际活跃任务接收 steer，空闲任务接收 start。注入丢失并杀进程后，在新连接上用原 clientId 和内容 hash 找回历史，重复投递零新增 RPC。首轮全场景分别约 1.69/2.60 s。[active](acceptance/runs/intent-notification/active.json) · [idle](acceptance/runs/intent-notification/idle.json) · [独立复跑](acceptance/runs/intent-notification-repeat/) |
| Job 收到后启动回执丢失 | 后端真实进程继续执行。GX 按原 operation key 和进程收据找到同一 Job，恢复约 0.87 s，没有重复启动。[存储和进程回执](acceptance/runs/jobs/lost-launch-ack.json) |
| Intent 与 Job 终态同时写入 | 两个独立进程实际争 SQLite 写锁，分别制造两种提交顺序。清单如实记录完成时的约束资格，随后两种情况均保留原始哈希并拒绝违反新禁令的当前证据。[变更先](acceptance/runs/intent-races/test_change_wins_job_completion_race/ordering.json) · [Job 先](acceptance/runs/intent-races/test_job_completion_wins_change_race/ordering.json) |
| 旧 Thread 在 fencing 后恢复 | 官方恢复旧 Root 后提交产品被 GX 拒绝，新增接受记录为 0。[真实回调](acceptance/runs/native/gx_fence.json) |
| 重复 Outbox 与终态重复启动 | 返回同一回执；正式项目结束后重复打开，新增 Job、模型请求、接受提交均为 0。[通知重放](acceptance/runs/native/gx_delivery_loss.json) · [完整项目复开](acceptance/runs/e2e-grid/summary.json) |
| 长 shell 活动时 steer | 6 个固定两秒命令中，正确 steer 的 ACK 约 1.5–2.2 ms；模型在约 1.90–1.93 s 后才收到输入。GX 在事务中先执行 fencing。[逐次记录](audit/NATIVE_ACCEPTANCE.md) |
| 压缩/重启/恢复与 provider 不匹配 | 两条 provider 协议路径在压缩后恢复并 fork，任务书和动态工具在真实调用中可见。已加载时核对有效 provider，冷恢复错配报 -32600。[OpenAI 路径](acceptance/runs/native/compact_openai.json) · [本地其他 provider](acceptance/runs/native/compact_local.json) · [错配](acceptance/runs/native/provider.json) |
| 等待 Job 时 Root 被杀 | Goal 进入 paused，等待期间零推理。真实 detached CPU Job 完成后从原 Root 唤醒，重复 wake 返回同一回执。[等待、重启与唤醒](acceptance/runs/native/gx_job_wait.json) |
| 多 Root 争抢限额 | 第一笔占满资源时第二笔在首次模型请求前被拒绝；释放余量后回到原 Root 执行一次。没有额外预留。[实际两线程](acceptance/runs/e2e-contention/evidence.json) |

早期运行暴露过两项问题。终态释放尚未收到回执的模型预算，导致一笔 210 token 漏记；空 Root 尚未写入 rollout 时，读取历史也曾引起 -32603。原始失败保留，修正后在独立状态重复运行。最终总账与 1,400 个脚本 token 对齐。[原费用差额](acceptance/runs/e2e-before-final-charge-grid/) · [原冷恢复记录](acceptance/runs/e2e-before-goal-read-kill/) · [修复后验证](acceptance/runs/e2e-kill/summary.json)
