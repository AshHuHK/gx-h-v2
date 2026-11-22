# P0 验收交叉核对

集成证据使用固定版本 Codex 0.169.0。下表中的本机 Responses 服务按脚本回答，官方二进制、原生线程、stdio 回调、SQLite、CPU 子进程、执行清单和文件哈希均实际运行。独立 reviewer 和 planner 指不同的根线程、任务和提交主体。科研结论的范围限于给定数值夹具。本机测试没有付费模型调用。

## 复现与读数

在包根运行，每个输出路径使用新目录。前两条命令调用公开的 `bin/gx-p0`。第三条针对两个官方 Root 同时争用预算时的准入边界。

```sh
python3 -B acceptance/e2e_grid.py --output /tmp/gx-e2e-normal --timeout 600
python3 -B acceptance/e2e_grid.py --interrupt-worker --output /tmp/gx-e2e-kill --timeout 90
python3 -B acceptance/e2e_contention.py --out /tmp/gx-e2e-contention/evidence.json
```

| 路径 | 实际结果 | 费用和重开 |
|---|---|---|
| 操作员 start / stop / status / resume | 两个初始 Job 的 ID、后端标识一直保留。三个科研轮次、四个 CPU Job、六个不同 worker Root、58 份产物均经核对。 | 40 个本机 Responses 请求、7 笔原生 turn 结算、1,400 个脚本 token；重开后 0 个模型请求、0 个新 Job、0 个新提交。[命令与账本](runs/e2e-grid/summary.json) |
| 正在审查时杀 App Server | 原 CLI 自动重连并在同一 Thread/Attempt 完成审查。从注入到科研自然停止为 4.372 秒，之后两次重进都没有新增科学结果。 | 共接受 41 个本机请求，1 个在故障中没有收到应答。40 笔应答、8 笔 turn 结算、1,400 个脚本 token。外部模型费用和 GPU 用量均为 0。[故障](runs/e2e-kill/fault.json)、[对账](runs/e2e-kill/summary.json) |
| 两个官方 Root 争用预算 | 100-token 预留阻止第二个 Turn。释放第一笔余量后，相同的第二 Root 获得 65-token 预留。 | 两个实际应答、合计 70 个脚本 token，最终预留为 0。[预算快照](runs/e2e-contention/evidence.json) |

科研夹具先在 x = 1.5、2、2.5 与 −2.5、−2、−1.5 上比较 −0.25 阈值。前者有样本达标，后者三个实测值都低于阈值。继续路线在 1.75 与 2.25 观察到 −0.0625；后继预测改测 1.625 与 1.875，得到 −0.140625 与 −0.015625，在预设三轮上限停止。脚本仅从真实线程工具返回的内容选择下一步。验收端按输入哈希和 `-(x-2)^2` 对每个数值重新计算。[模型夹具](e2e_responses.py)、[独立核算](e2e_audit.py)、[原始观测](runs/e2e-grid/04-before-replay.json)

## 协议、授权与科研

| ID | 观测或校验 | 记录 |
|---|---|---|
| C-01 | 已初始化服务再次 initialize 返回 -32600。正常握手后可发业务 RPC。 | [协议收发](runs/native/session.json) |
| C-02 | 在官方线程创建、运行、重启服务及恢复后，读到原有轮次并追加新轮次。 | [会话](runs/native/session.json) |
| C-03 | 连续接收 thread/turn/item 事件，故意丢通知后可按历史核对。 | [事件与恢复](runs/native/session.json) |
| C-04 | 实测创建、获取、使用量、替换、清除 Goal。活跃任务能看到更改，模型自行替换目标受到限制。 | [Goal](runs/native/goal.json)、[目标更新](runs/native/goal_update.json) |
| C-05 | 持有真实异步 CPU Job 的根线程暂停 Goal，等待期间零模型请求；Job 完成后在原根线程唤醒一次。 | [任务、Job、等待和唤醒](runs/native/gx_job_wait.json) |
| C-06 | 官方主线程关闭 Goal；通过工具目录与模型实际调用结果核验。 | [主线程](runs/native/main.json) |
| C-07 | 长 shell 执行中，使用正确的 expectedTurnId 接受 steer；错误 ID 受拒。每个样本同时记录 RPC 回执和实际模型收到时间。 | [延迟样本](runs/native/shell.json) |
| C-08 | 空闲线程拒绝 steer，然后成功 turn/start。空闲状态与 GX 投递回执另有恢复校验。 | [官方会话](runs/native/session.json)、[GX 投递](runs/native/gx_delivery_loss.json) |
| C-09 | 官方原生子代理有独立 ID，继承父级只读沙箱；对照测试了默认与显式动态工具继承。GX 计入后代用量。 | [原生子代理](runs/native/child.json)、[默认工具](runs/native/child_default_tools.json)、[子代理用量](runs/native/gx_child_meter.json) |
| C-10 | 原生服务中断后恢复相同 Root 和 Attempt，保留先前工作。输入前失联与输入后失联单独覆盖。 | [官方重启](runs/native/kill.json)、[GX 建线程响应丢失](runs/native/gx_create_loss.json)、[产品科研链](runs/e2e-kill/summary.json) |
| I-01 | 脚本化操作员经公开 CLI 在一个官方主线程上提交，保留不同的真实 turn ID。 | [命令与 ID](runs/intent-cli-confirmations/summary.json) |
| I-02 | 从动作参数伪造授权被拒，已关闭租约的原根线程实际工具回调也被拒。 | [伪造动作](runs/intent-cli-confirmations/summary.json)、[过期线程](runs/native/gx_fence.json) |
| I-03 | 含糊消息形成 proposal，版本历史可逐项追踪。 | [CLI 状态快照](runs/intent-cli-confirmations/database.json) |
| I-04 | Supplement 记录了新增信息，既有 Intent Item 版本保持原值。 | [版本历史](runs/intent-cli-confirmations/database.json) |
| I-05 | 明确的结构化改动写入 Change Record 和条目版本。 | [变更及主线程关联](runs/intent-cli-confirmations/database.json) |
| I-06 | 解除 method_ban 需一次预览和后续独立主线程输入确认。 | [两次输入记录](runs/intent-cli-confirmations/database.json) |
| I-07 | Objective 替换同样需要后续确认，伪造或修改过的预览受拒。 | [操作员命令](runs/intent-cli-confirmations/summary.json) |
| I-08 | 降低结构化额度立即生效，提高额度须后续主线程确认。物理 provider HTTP 调用次数硬上限明确报为不支持。 | [实际主线程](runs/intent-cli-confirmations/database.json)、[上限单位](runs/jobs/native-provider-hard-limit.json) |
| I-09 | 对照相关与无关对象；按保存的执行/评估条目依赖产生相应处置，同时保留原始产物。 | [影响快照](runs/intent-auth/test_item_impact_refresh_notify_interrupt.json)、[并发变更](runs/intent-races/test_change_wins_job_completion_race/after.json) |
| I-10 | 在评估提交前、后分别改变判据，按原 observations_ref 新建评估，Job 数不增加。 | [先有产物](runs/science/test_changed_criterion_reuses_original_bytes_and_job.json)、[先改标准](runs/science/test_criterion_changes_before_original_evaluation_commits.json) |
| I-11–I-13 | SQLite 变更与 Outbox 原子提交；受影响线程变更通知失败后恢复，以原投递键去重。 | [提交中被杀](runs/intent-races/test_killed_change_writer_rolls_back_items_impact_and_outbox/recovery.json)、[活跃线程通知](runs/intent-notification/active.json)、[空闲线程通知](runs/intent-notification/idle.json) |
| I-14 | 同一真实 Job 完成和 Intent 变更按两个顺序提交；两种顺序均保留历史并按当前资格判断。 | [变更先提交](runs/intent-races/test_change_wins_job_completion_race/ordering.json)、[Job 先完成](runs/intent-races/test_job_completion_wins_change_race/ordering.json) |
| T-01 | 原线程恢复后试图正式提交，服务器按当前租约拒绝；没有额外 submission。 | [原线程与拒绝回执](runs/native/gx_fence.json) |
| T-02 | 后端接受 Job 后模拟调用者丢失回执，按原 scheduler receipt 对账、零额外启动。 | [Job 后端记录](runs/jobs/lost-launch-ack.json) |
| T-03 | 原生线程所在进程被终止，已脱离的 CPU Job 继续完成，另起进程恢复原任务。 | [本地 Job 生命周期](runs/native/gx_job_wait.json) |
| T-04 | 实际网格准入拒绝被禁能力/超 CPU 限额；两个真实线程竞争同一 100-token 额度，首个持有期间第二个未获得 turn。 | [能力与输出](runs/jobs/hard-constraint-and-external-output.json)、[项目预算](runs/jobs/project-resource-ledger.json)、[线程竞争](runs/e2e-contention/evidence.json) |
| E-01 | 四个实际 Job 均持有带输入、参数、能力版本、环境、限额和完成信息的清单。另有后端未确认前的 admission 记录。 | [执行清单](runs/e2e-grid/04-before-replay.json)、[初始 admission](runs/jobs/lost-launch-ack.json) |
| E-02 | 独立读取并比对每份 artifact 的 SHA-256 和大小，重算执行清单哈希；修改暂存文件或引入外部产物受到检测。 | [哈希审计](runs/e2e-grid/04-before-replay.json)、[篡改对照](runs/jobs/staging-replacement.json) |
| E-03 | 每一条 Evidence 均关联当前 Job 的原始观测、冻结规范的精确哈希及评估结果。 | [settlements 和 manifest](runs/e2e-grid/04-before-replay.json) |
| E-04 | 失败或无效的测量没有支持/反驳 Evidence；同项目其他有效测量保留。 | [四类结算快照](runs/science/test_four_classes_and_reviewed_evidence.json) |
| E-05 | 三轮共六个不同 Root 负责评审与决策；每轮均对照真实 thread ID、lease principal 和 accepted submission ID。 | [独立主体与回执](runs/e2e-grid/04-before-replay.json) |
| E-06 | 在真实持久层和工作流 validator 上覆盖基础设施、代理、测量失败和有效结果。负向测试用可重复的输入驱动验证器。 | [四类结算](runs/science/test_four_classes_and_reviewed_evidence.json)、[负向测试代码](../gx-h/tests/test_p0_science.py) |
| R-01 | 两个初始预测，三轮实际 CPU 计算，共四个已完成 Job。操作员暂停、原状态恢复，再执行第四个 Job，均自动推进。 | [公开 CLI](runs/e2e-grid/summary.json)、[各轮原始观测](runs/e2e-grid/04-before-replay.json) |
| R-02 | 最后决定 stop；重开终态 run 没有新模型请求、Job、提交或科学结果。测试逐次核对最终原生用量和账本。 | [终态重开](runs/e2e-grid/summary.json)、[重开后快照](runs/e2e-grid/06-after-replay.json) |
| R-03 | 第 0 轮 continue+prune；第 1 轮 pivot 到新的有限网格预测；第 2 轮确实执行后继路线并 stop。所有分数按原始 x 重新计算。 | [决策及后继路线](runs/e2e-grid/04-before-replay.json) |

## 故障注入索引

| # | 注入与恢复结果 | 实际记录 |
|---|---|---|
| 1 | 在 GX SQLite 变更事务中杀进程，重新打开后原子回滚，重放后仅一份正式变更。 | [事务前、后与重放](runs/intent-races/test_killed_change_writer_rolls_back_items_impact_and_outbox/recovery.json) |
| 2 | 第 0 轮 reviewer 已读取两个 Job 的观测，在首次提交前杀掉官方 App Server，恢复原 lease/Thread 后继续研究。 | [CLI 故障时间线](runs/e2e-kill/fault.json)、[结果与费用](runs/e2e-kill/summary.json) |
| 3 | thread/start 已接受，GX 尚未写入 ID 时杀服务。未曾收到输入的空线程可能不出现在索引中；保留原创建记录后重建空上下文。 | [官方索引行为](runs/native/create_loss.json)、[GX 恢复](runs/native/gx_create_loss.json) |
| 4 | 丢失成功请求后的通知或回执，读取官方 turn/history，按 clientId 复用原 Turn。 | [原事件](runs/native/session.json)、[恢复与重放](runs/native/gx_delivery_loss.json) |
| 5 | Job 后端已接受，故意中断确认；返回同一个 Job 与后端标识。 | [回执丢失](runs/jobs/lost-launch-ack.json) |
| 6 | 两种确定性事务排序都在含真实 Job 输出的 GX 库上运行。 | [变更先](runs/intent-races/test_change_wins_job_completion_race/ordering.json)、[Job 先](runs/intent-races/test_job_completion_wins_change_race/ordering.json) |
| 7 | Fence 后恢复旧原生线程，实际调用 GX 写工具受拒。 | [过期根线程](runs/native/gx_fence.json) |
| 8 | 重放同一投递事件，保留单一生效回执。 | [Outbox 活跃投递重放](runs/intent-notification/active.json)、[空闲投递重放](runs/intent-notification/idle.json) |
| 9 | 本地 shell command 执行期间 steer；检测接收延迟、模型观测延迟与错误 expectedTurnId。 | [三次计时](runs/native/shell.json) |
| 10 | 两类 provider 配置均走官方压缩路径，然后 resume/fork，核查任务书与动态工具可见性。 | [本地压缩](runs/native/compact_local.json)、[OpenAI 路径的回环集成](runs/native/compact_openai.json) |
| 11 | 不存在的 provider 配置被拒，已加载忽略变更与严格冷恢复的 provider 行为分别留存。 | [provider 错误](runs/native/provider.json) |
| 12 | 第一 Root 冻结预算；第二个真实 Root 仍保留原身份，待返还额度后以剩余 65 token 获准，两次实际消费合计 70 token。 | [争用与恢复](runs/e2e-contention/evidence.json) |

## 首轮发现及修复对照

公开 CLI 首次完成三轮时，项目过早释放最后一个 native turn 的预留。单独核账发现 210 个测试 token 漏记。另一次资源争用测试触发官方空 rollout 读取竞态，故障恢复还暴露了验收夹具读取缺少 call_id 的旧输入项的问题。失败记录保留，可与同一测试入口的修复后运行比对。[首次核账](runs/e2e-before-final-charge-grid/summary.json)、[故障重启时的同类漏记](runs/e2e-before-final-charge-kill/summary.json)、[空 rollout](runs/e2e-contention/empty-rollout-before-fix.json)

首轮出现的 Goal 投影空 rollout 和脚本历史项差异也保留了诊断输出。[Goal 投影错误](runs/e2e-before-goal-read-kill/faulted-run.stderr)、[夹具异常](runs/e2e-before-peer-history-fix/requests.jsonl)
