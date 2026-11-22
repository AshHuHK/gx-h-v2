# 官方 Codex App Server 运行时验收

测试日期为 2026-11-22 UTC。Linux 原生二进制来自锁定的官方 `@openai/codex@0.169.0` 发行包，SHA-256 为 `b452ca20f838a8855a88a41050ee4a694033fff4ea3ecdad5633e6e0e8d13f2b`。上游 commit 为 `d32063f96c2dff1dc96af839b3952abb6acfeda4`。[发行物核对](../acceptance/runs/schema/cli-provenance.json) · [锁定源码](CODEX_SOURCE.md)

在包根目录运行以下命令。每个场景使用独立临时 Codex 主目录，GX 故障场景使用真实 SQLite。退出码、时间、原生 thread/turn ID、双向协议事件、回环模型请求、用量和 GX 状态快照分别保存在运行记录中。[命令输出](../acceptance/runs/native/command.log) · [协议驱动](../acceptance/native_probe.py) · [回环 Responses 服务](../acceptance/native_probe_peer.py) · [GX 故障注入](../acceptance/native_probe_roots.py)

```bash
python -B acceptance/native_probe.py
```

**17 个场景在原始验收与独立复跑均通过，两个进程均以 0 退出。** 每个实际模型请求都记录并断言 wire reasoning effort 为 xhigh。协议身份为本地确定性服务，在协议层声明的模型名为 gpt-5.4 或 gpt-6.1-sol。

**实测边界。** 官方二进制执行了线程、子 Agent、工具调用、上下文压缩、Goal 记账及持久化。测试控制的 Responses 服务在回环地址发送确定性 SSE，提供输入、输出 token 计数并记录下一请求何时包含预设标记。两种提供方路径均使用此服务。压缩场景验证摘要协议和任务书重插，摘要质量、生产模型推理质量与云端计费需分别检验。真实 OAuth 服务的模型目录、effort 与回包有独立记录。[Responses 服务](../acceptance/native_probe_peer.py) · [OAuth 实测](OAUTH_EFFORT.md)

| 矩阵项 | 操作、断言和观测 | 逐项证据 |
|---|---|---|
| C-01～C-03 | 初始化前的普通请求和重复 initialize 均被拒绝。正向路径发送 initialize、接收 ACK、发送 initialized，再创建 thread 和 turn。持续读取 thread/turn/item 事件，以单条分页的 thread/items/list 对照同一 turn 的所有 item ID、started/completed 事件和重启恢复的历史。 | [session](../acceptance/runs/native/session.json) |
| C-04 | 建立 Goal 后读取、修改、清除、重新建立。现有 Goal 改写 objective 保留已消费 token 和预算。清除后新建从 0 开始。运行中外部修改会进入下一模型请求。模型调用 update_goal 时附带的任意 objective 未能改变客户端设定的 objective。 | [goal](../acceptance/runs/native/goal.json) · [goal_update](../acceptance/runs/native/goal_update.json) |
| C-05、T-03 | 将实际运行中的 GX grid Job 交给独立 runner，调用 wait_job 并观测 Goal 为 paused。初始 turn 完成后杀死 App Server。后台 Job 完成、对账、在新 App Server 中恢复原 root 并以新 turn 唤醒。等待期间新增模型请求 0 次。相同 wake event 的活动期及结算后重放均核对原回执。 | [gx_job_wait](../acceptance/runs/native/gx_job_wait.json) |
| C-06 | 以 thread/start 的 config 设置 features.goals=false。模型收到的工具列表无 Goal，强行调用 create_goal 收到 unsupported，存储中无 Goal。进程全局仍可显式调用 Goal RPC。在该主线程外部投影 active Goal 后记录一个空闲观测窗的自动模型请求数。 | [main](../acceptance/runs/native/main.json) |
| C-07、故障 9 | 三个 turn 分别执行固定 POSIX sleep 2 和 echo，收到 commandExecution started 后发错误及正确的 expectedTurnId。错误 ID 被拒绝。正确 steer 的 ACK 数毫秒即返回，模型读到 steer 时命令已接近或达到两秒。每次退出码 0，终端输出含预期标记。容器禁止只读 bwrap 的 mount 操作，因此这三个限定命令采用单独的 danger-full-access 测试 thread。 | [shell 的三个独立样本](../acceptance/runs/native/shell.json) |
| C-08 | 空闲时 turn/steer 返回 -32600，随后 turn/start 得到不同的 turn ID 和一次实际工具回调。 | [session](../acceptance/runs/native/session.json) |
| C-09 | 原生 spawn_agent 创建另一 thread。父、子有效配置均为 readOnly、networkAccess=false、approvalPolicy=never。同一无 fork 历史的新 child，在启用 multi_agent_v2_dynamic_tools 时能实际调用父动态工具，关闭到默认配置后得到 unsupported。恢复父 thread 后原生 followup_task 再次从同一 child ID 调用已继承工具。 | [子线程与恢复](../acceptance/runs/native/child.json) · [默认配置对照](../acceptance/runs/native/child_default_tools.json) |
| C-10、故障 2 | 模型回复悬挂时 SIGKILL 官方服务，冷启动 thread/resume 返回原 thread 和已中断的 turn。同一 thread 随后完成一条新指令。对另一个明确指定的活动 turn 调用官方 turn/interrupt，收到 interrupted 终态。另一个 bounded shell 在 SIGKILL 前已留下 started 文件，2.2 秒后记录 finished 文件与官方历史状态。 | [kill](../acceptance/runs/native/kill.json) · [kill_shell](../acceptance/runs/native/kill_shell.json) |
| 故障 3 | 控制器在 thread/start ACK 回来时切断，GX 无 thread ID。初始官方接口探针测得首次输入前死亡的空 thread 可从索引消失。产品探针让 GX 留存创建意图，核实原进程已死并对账，只在原空线程从未收过业务输入时替换。账内仅一份有效 root、一次业务 turn、一次预算预留。 | [官方索引实测](../acceptance/runs/native/create_loss.json) · [GX 状态与恢复](../acceptance/runs/native/gx_create_loss.json) |
| I-12～I-13、故障 4、8 | 同时丢弃 turn/start 回复与全部应用层通知，SIGKILL 后通过官方分页历史的 clientId 和原始文本摘要定位同一输入。相同 event 再重放只返回原 turn，无额外模型请求、预算预留或正式动作。 | [GX 状态、历史和协议日志](../acceptance/runs/native/gx_delivery_loss.json) |
| T-01、故障 7 | 旧 lease 结束后真实恢复该 Codex thread，模型执行 gx_submit_product，GX 动态工具返回失效租约错误。正式提交表无新增记录。 | [gx_fence](../acceptance/runs/native/gx_fence.json) |
| 故障 10 | 非 OpenAI 配置触发本地摘要。OpenAI 分支向 responses 发送 compaction_trigger 并接回加密摘要。手动 compact 后、重启 resume 后、fork 后的三次实际模型请求都带有对应摘要、developer 任务书标记与动态工具。session 场景还在冷恢复后实际调用持久化的动态工具。 | [本地摘要](../acceptance/runs/native/compact_local.json) · [OpenAI 协议分支](../acceptance/runs/native/compact_openai.json) · [动态工具恢复](../acceptance/runs/native/session.json) |
| 故障 11 | 未配置 provider 被拒绝。在已加载的 local thread 上带另一 provider 的 resume 可成功，但响应仍报告原 provider。在全新进程以 allowProviderModelFallback=false 尝试切换时，官方返回 -32600 并明确说明不得更换 provider。该错误的 data 为空。 | [provider](../acceptance/runs/native/provider.json) |

**父子预算口径。** 子线程 fixture 每次上报 183 tokens，父线程每次上报 35。读取父 Goal 的快照已包含子线程消费。根和后代独立请求均在轨迹中可数，Goal 是活动窗口的快照，结果会随最后一轮计量进展。重复加总父 Goal 和同一批子线程用量会重复计费。源码按未缓存输入和输出计算 Goal 增量，且维护后代增量。[运行记录](../acceptance/runs/native/child.json) · [计量实现](../vendor/codex/codex-rs/ext/goal/src/accounting.rs#L570)

**源码与部署限制。** 锁定协议定义了 modelProviderMismatch 结构。这里对本地服务的实测覆盖了 provider 拒绝及加载中保留原 provider，未观测到该命名错误的结构化返回。已执行的每次网络模型请求均由回环服务处理，推理 GPU 和外部 API 开销为零。读回故障时长反映本机样本；命令丢回执、终止信号与业务 Job 的结算应以各自测试中的持久回执核对。[错误定义](../vendor/codex/codex-rs/app-server-protocol/src/protocol/v2/model_provider_error.rs#L22) · [原始结果目录](../acceptance/runs/native)

## 重复运行与时延

两次独立运行分别记录 55、55 个本地 Responses 请求，脚本分别为这些请求指定 2813、2813 tokens。这些计数包含了故障切断时已经到达回环服务的请求。两个进程使用各自的 SQLite、Codex 主目录和线程 ID。[首次运行](../acceptance/runs/native/command.log) · [独立复跑](../acceptance/runs/native-repeat/command.log)

对六个独立的两秒 shell 样本，从**正确**的 steer 发出到 JSON-RPC ACK 用时 1.5–2.2 ms，中位数 1.85 ms。到模型下一次请求携带对应样本唯一标记为 1.9007–1.9288 秒。每次先执行并收到错误 expectedTurnId 的负向检查，再开始正向计时。原生 tool 与模型的步界延迟和协议确认延迟可以分别观测。[首次三样本](../acceptance/runs/native/shell.json) · [复跑三样本](../acceptance/runs/native-repeat/shell.json)
